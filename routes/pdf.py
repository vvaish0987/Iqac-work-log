from flask import Blueprint, request, redirect, flash, session, send_file
import os
import html as _html
from datetime import datetime
from io import BytesIO
from db import get_db_connection, get_cursor
import cloudinary
import cloudinary.uploader

def esc(s):
    """HTML-escape a string so ReportLab's XML parser doesn't choke on & < > characters."""
    if s is None:
        return ''
    return _html.escape(str(s))

def _cloudinary_upload_ws(file_obj, username, reporting_month, index):
    public_id = f"{username}/{reporting_month}/workshop_{index + 1}"
    result = cloudinary.uploader.upload(file_obj, folder="iqac/workshop_attachments", public_id=public_id, resource_type="auto", access_mode="public", overwrite=True)
    return result["secure_url"], result["public_id"]

# ReportLab for PDF generation
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, HRFlowable, PageBreak, KeepTogether
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

from werkzeug.datastructures import MultiDict

def sort_list_fields(form_data, report_type, ws_files=None):
    def sort_section(date_key, keys):
        dates = form_data.get(date_key) or []
        if not isinstance(dates, list):
            dates = [dates] if dates else []
        if not dates:
            return None
            
        max_len = len(dates)
        lists = {}
        for k in keys:
            vals = form_data.get(k) or []
            if not isinstance(vals, list):
                vals = [vals] if vals else []
            lists[k] = vals
            if len(vals) > max_len:
                max_len = len(vals)
                
        ws_file_objs = []
        if date_key == "ws_date[]" and ws_files:
            ws_file_objs = ws_files
            
        rows = []
        for i in range(max_len):
            row = {}
            for k in keys:
                row[k] = lists[k][i] if i < len(lists[k]) else ""
            if date_key == "ws_date[]" and ws_files:
                row["_file"] = ws_file_objs[i] if i < len(ws_file_objs) else None
            rows.append(row)
            
        def get_date_val(r):
            v = r.get(date_key) or ""
            return v.strip()
            
        rows.sort(key=lambda r: (1 if not get_date_val(r) else 0, get_date_val(r)))
        
        for k in keys:
            form_data[k] = [r[k] for r in rows]
            
        if date_key == "ws_date[]" and ws_files:
            return [r["_file"] for r in rows]
        return None

    if report_type == "aqar_coordinator":
        sort_section("act_date[]", ["act_date[]", "act_task[]", "act_area[]", "act_area_other[]", "act_stakeholders[]", "act_outcome[]", "act_status[]"])
        sort_section("meet_date[]", ["meet_date[]", "meet_programme[]", "meet_role[]", "meet_outcome[]"])
    elif report_type == "standard":
        sort_section("meeting_date[]", ["meeting_date[]", "dept_name[]", "participants[]", "topics[]", "action_points[]", "responsibility_area[]"])
        sorted_ws_files = sort_section("ws_date[]", ["ws_date[]", "ws_venue[]", "ws_title[]", "ws_participants[]", "ws_resource[]", "ws_responsibility[]", "ws_existing_file[]"])
        return sorted_ws_files
    return None

pdf_bp = Blueprint('pdf', __name__)

@pdf_bp.route("/iqac_monthly_report/download", methods=["POST"])
def iqac_monthly_report_download():
    if "username" not in session:
        return redirect("/login")

    username = session["username"]
    conn = get_db_connection()
    cursor = get_cursor(conn)

    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cursor.fetchone()

    if not user or user["role"].lower() not in ("school iqac coordinator", "campus iqac coordinator"):
        conn.close()
        flash("Access denied.", "danger")
        return redirect("/login")

    if not REPORTLAB_AVAILABLE:
        conn.close()
        flash("PDF generation library (reportlab) is not installed on the server.", "danger")
        return redirect("/iqac_monthly_report")

    reporting_month = request.form.get("reporting_month", "report")

    # Check if report is locked
    cursor.execute("""
        SELECT status FROM signed_reports 
        WHERE username=%s AND reporting_month=%s
    """, (username, reporting_month))
    signed_row = cursor.fetchone()
    if signed_row and signed_row["status"] in ('pending_upload', 'uploaded', 'reviewed'):
        conn.close()
        flash("This report is locked because the PDF has been generated/submitted. No modifications are allowed.", "danger")
        return redirect("/iqac_dashboard")

    # ── Auto-save draft on download ──
    import json
    form_data_obj = {}
    for key in request.form.keys():
        if key.endswith('[]'):
            form_data_obj[key] = request.form.getlist(key)
        else:
            form_data_obj[key] = request.form.get(key)

    ws_files = request.files.getlist("ws_report_file[]")
    sorted_ws_files = sort_list_fields(form_data_obj, "standard", ws_files) or ws_files

    aqar_emails_env = os.getenv("AQAR_COORDINATOR_EMAILS", "")
    aqar_emails = [e.strip().lower() for e in aqar_emails_env.split(",") if e.strip()]
    email = (user.get("email") or "").strip().lower()
    report_type = "aqar_coordinator" if email in aqar_emails else "standard"

    try:
        cursor.execute("""
            INSERT INTO report_drafts (username, report_type, reporting_month, form_data, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (username, report_type, reporting_month)
            DO UPDATE SET form_data = EXCLUDED.form_data, updated_at = CURRENT_TIMESTAMP
        """, (username, report_type, reporting_month, json.dumps(form_data_obj)))
        conn.commit()
    except Exception as e:
        print("Error saving draft:", str(e))
        conn.rollback()

    ws_titles = form_data_obj.get("ws_title[]") or []
    ws_existing_files = form_data_obj.get("ws_existing_file[]") or []
    num_rows = len(ws_titles)
    ws_attachments = []  # list of (index, cloudinary_url, filename)

    try:
        # Load existing workshop attachments from DB for this user/month
        db_conn2 = get_db_connection()
        db_cur2 = get_cursor(db_conn2)
        db_cur2.execute("""
            SELECT workshop_index, filename, cloudinary_url, cloudinary_public_id
            FROM workshop_attachment_files
            WHERE username=%s AND reporting_month=%s
        """, (username, reporting_month))
        existing_ws = db_cur2.fetchall()
        db_conn2.close()

        new_db_records = []

        for i in range(num_rows):
            uploaded_file = sorted_ws_files[i] if i < len(sorted_ws_files) else None
            existing_name = ws_existing_files[i] if i < len(ws_existing_files) else ""

            if uploaded_file and uploaded_file.filename:
                try:
                    cld_url, cld_pid = _cloudinary_upload_ws(uploaded_file, username, reporting_month, i)
                    new_db_records.append({
                        "workshop_index": i,
                        "filename": uploaded_file.filename,
                        "cloudinary_url": cld_url,
                        "cloudinary_public_id": cld_pid
                    })
                    ws_attachments.append((i, cld_url, uploaded_file.filename))
                except Exception as e:
                    print(f"Workshop upload error row {i}: {e}")
            elif existing_name:
                # Find the matching record from existing_ws
                matching = None
                for rec in existing_ws:
                    if rec["filename"] == existing_name:
                        matching = rec
                        break
                if matching:
                    new_db_records.append({
                        "workshop_index": i,
                        "filename": matching["filename"],
                        "cloudinary_url": matching["cloudinary_url"],
                        "cloudinary_public_id": matching["cloudinary_public_id"]
                    })
                    ws_attachments.append((i, matching["cloudinary_url"], matching["filename"]))

        # Clear and re-insert workshop attachment records with sorted indices
        db_conn3 = get_db_connection()
        db_cur3 = get_cursor(db_conn3)
        db_cur3.execute("""
            DELETE FROM workshop_attachment_files
            WHERE username=%s AND reporting_month=%s
        """, (username, reporting_month))

        for rec in new_db_records:
            db_cur3.execute("""
                INSERT INTO workshop_attachment_files (username, reporting_month, workshop_index, filename, cloudinary_url, cloudinary_public_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (username, reporting_month, rec["workshop_index"], rec["filename"], rec["cloudinary_url"], rec["cloudinary_public_id"]))

        db_conn3.commit()
        db_conn3.close()
    except Exception as e:
        print(f"Workshop attachment processing error: {e}")

    # Construct MultiDict from sorted form_data_obj to pass to _generate_iqac_pdf
    sorted_multi_form = MultiDict()
    for k, v in form_data_obj.items():
        if isinstance(v, list):
            for val in v:
                sorted_multi_form.add(k, val)
        else:
            sorted_multi_form.add(k, v)

    try:
        pdf_buffer = _generate_iqac_pdf(sorted_multi_form, ws_attachments)
    except Exception as e:
        import traceback
        print(f"PDF generation error:\n{traceback.format_exc()}")
        conn.close()
        flash("PDF generation failed. Please try again.", "danger")
        return redirect("/iqac_monthly_report")

    # Only mark pending_upload AFTER successful PDF generation
    try:
        cursor.execute("SELECT status FROM signed_reports WHERE username=%s AND reporting_month=%s", (username, reporting_month))
        existing_report = cursor.fetchone()
        if not existing_report:
            cursor.execute("INSERT INTO signed_reports (username, reporting_month, status) VALUES (%s, %s, 'pending_upload')", (username, reporting_month))
        else:
            cursor.execute("UPDATE signed_reports SET status='pending_upload', remarks=NULL, uploaded_file_path=NULL WHERE username=%s AND reporting_month=%s", (username, reporting_month))
        conn.commit()
    except Exception as e:
        print("Error updating signed_reports status:", str(e))
        conn.rollback()
    conn.close()

    full_name = (user.get("full_name") or username).strip()
    try:
        month_label = datetime.strptime(reporting_month, "%Y-%m").strftime("%B")
    except Exception:
        month_label = reporting_month
    safe_name = "".join(c if c.isalnum() or c in (' ', '-') else '' for c in full_name).strip()
    filename = f"{safe_name} {month_label} IQAC Report.pdf"

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


def _generate_iqac_pdf(form_data, ws_attachments=None):
    """Generate the IQAC Monthly Report PDF and return a BytesIO buffer."""
    buffer = BytesIO()

    usable_width = A4[0] - 4 * cm  # 2cm margins each side

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=0.5 * cm,
        bottomMargin=2 * cm
    )

    styles = getSampleStyleSheet()

    accent      = colors.HexColor('#1F497D')   # dark navy — text only, no backgrounds
    tbl_header  = colors.HexColor('#BDD7EE')   # light blue — section banners, table headers, info labels
    light_blue  = colors.HexColor('#BDD7EE')   # same light blue — info label cells (unified)
    alt_row     = colors.white                  # white — all data rows

    def make_style(name, size=9, bold=False, align=TA_LEFT, space_before=0, space_after=4, italic=False, text_color=None):
        fname = 'Times-Roman'
        if bold and italic:
            fname = 'Times-BoldItalic'
        elif bold:
            fname = 'Times-Bold'
        elif italic:
            fname = 'Times-Italic'
        kwargs = dict(parent=styles['Normal'], fontSize=size, fontName=fname,
                      alignment=align, spaceBefore=space_before, spaceAfter=space_after)
        if text_color:
            kwargs['textColor'] = text_color
        return ParagraphStyle(name, **kwargs)

    small = make_style('small', size=7.5)

    def format_date(d_str):
        if not d_str:
            return ''
        try:
            return datetime.strptime(d_str.strip(), '%Y-%m-%d').strftime('%d-%m-%Y')
        except Exception:
            try:
                return datetime.strptime(d_str.strip(), '%d/%m/%Y').strftime('%d-%m-%Y')
            except Exception:
                return d_str

    _sh_counter = [0]
    def section_header(text):
        _sh_counter[0] += 1
        t = Table([[Paragraph(text, make_style(f'sh_{_sh_counter[0]}', size=10, bold=True,
                                               space_after=0, align=TA_LEFT, space_before=0,
                                               text_color=accent))]],
                  colWidths=[usable_width])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), tbl_header),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
        ]))
        return t

    def table_style(has_header=True):
        ts = [
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
            ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]
        if has_header:
            ts += [
                ('BACKGROUND', (0, 0), (-1, 0), tbl_header),
                ('TEXTCOLOR', (0, 0), (-1, 0), accent),
                ('FONTNAME', (0, 0), (-1, 0), 'Times-Bold'),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ]
        return TableStyle(ts)

    elements = []

    # ── Header ──────────────────────────────────────────────────────────────
    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'christ_logo.png')
    if os.path.exists(logo_path):
        # 1794 x 608 aspect ratio ~ 2.95
        # Let's make the logo 5.9 cm wide and 2.0 cm high so it displays without distortion
        logo_width = 5.9 * cm
        logo_height = 2.0 * cm
        logo_image = RLImage(logo_path, width=logo_width, height=logo_height)
        
        # Pushing the logo to the far right using a Table
        logo_table = Table([['', logo_image]], colWidths=[usable_width - logo_width, logo_width])
        logo_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))
        elements.append(logo_table)
        elements.append(Spacer(1, 18))

    # ── Title Section ────────────────────────────────────────────────────────
    elements.append(Paragraph('Internal Quality Assurance Cell (IQAC)', make_style('h2', size=15, bold=True, align=TA_CENTER, space_after=10)))
    elements.append(Paragraph('IQAC Monthly Reports', make_style('h3', size=10, align=TA_CENTER, space_after=0)))
    elements.append(Spacer(1, 6))
    elements.append(HRFlowable(width=usable_width, thickness=2, color=accent, spaceAfter=10))

    # ── Header Info ─────────────────────────────────────────────────────────
    coord_name = esc(form_data.get('coordinator_name', ''))
    school = esc(form_data.get('school_campus', ''))
    rep_month_raw = form_data.get('reporting_month', '')
    try:
        rep_month_display = datetime.strptime(rep_month_raw, '%Y-%m').strftime('%m-%Y')
    except Exception:
        rep_month_display = rep_month_raw

    info_data = [
        [Paragraph('Name of the IQAC Coordinator:', make_style('lbl', bold=True, size=9, space_after=0)),
         Paragraph(coord_name, make_style('val', size=9, space_after=0)),
         Paragraph('Reporting Month:', make_style('lbl2', bold=True, size=9, space_after=0)),
         Paragraph(rep_month_display, make_style('val2', size=9, space_after=0))],
        [Paragraph('School/Campus:', make_style('lbl3', bold=True, size=9, space_after=0)),
         Paragraph(school, make_style('val3', size=9, space_after=0)),
         '', ''],
    ]
    w = usable_width
    info_table = Table(info_data, colWidths=[w * 0.28, w * 0.32, w * 0.18, w * 0.22])
    info_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
        ('BACKGROUND', (0, 0), (0, -1), light_blue),
        ('BACKGROUND', (2, 0), (2, -1), light_blue),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 8))

    # ── Section I ───────────────────────────────────────────────────────────
    # (header added after we know if there's any data)

    # Part (a)
    part_a_label = '(a) Meetings / Activities conducted relating to IQAC Coordinator\'s responsibility areas'

    meet_dates = form_data.getlist('meeting_date[]')
    dept_names = form_data.getlist('dept_name[]')
    participants = form_data.getlist('participants[]')
    topics = form_data.getlist('topics[]')
    action_pts = form_data.getlist('action_points[]')
    resp_areas = form_data.getlist('responsibility_area[]')

    pa_headers = ['Date of\nMeeting', 'Department\nName', "Participants'\nDetails",
                  'Topics\nDiscussed', 'Action Points\n/ Plan']
    pa_cols = [w * 0.13, w * 0.20, w * 0.22, w * 0.225, w * 0.225]

    pa_rows_filled = [(meet_dates[i] if i < len(meet_dates) else '').strip() or
                      (dept_names[i] if i < len(dept_names) else '').strip() or
                      (topics[i] if i < len(topics) else '').strip()
                      for i in range(len(meet_dates))]
    has_pa_data = any(pa_rows_filled)

    pa_data = [[Paragraph(h, make_style(f'ph{i}', size=7.5, bold=True, space_after=0, text_color=accent)) for i, h in enumerate(pa_headers)]]
    for i in range(len(meet_dates)):
        if not pa_rows_filled[i]:
            continue
        pa_data.append([
            Paragraph(format_date(meet_dates[i]) if i < len(meet_dates) else '', small),
            Paragraph(esc(dept_names[i]) if i < len(dept_names) else '', small),
            Paragraph(esc(participants[i]) if i < len(participants) else '', small),
            Paragraph(esc(topics[i]) if i < len(topics) else '', small),
            Paragraph(esc(action_pts[i]) if i < len(action_pts) else '', small),
        ])

    if has_pa_data:
        elements.append(section_header('Section I: Quality Assurance Initiatives'))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(part_a_label, make_style('parta', size=8, bold=True, space_after=4)))
        pa_table = Table(pa_data, colWidths=pa_cols, repeatRows=1)
        pa_table.setStyle(table_style())
        elements.append(pa_table)
        elements.append(Spacer(1, 6))

    ws_dates = form_data.getlist('ws_date[]')
    ws_venues = form_data.getlist('ws_venue[]')
    ws_titles = form_data.getlist('ws_title[]')
    ws_parts = form_data.getlist('ws_participants[]')
    ws_res = form_data.getlist('ws_resource[]')
    ws_resp = form_data.getlist('ws_responsibility[]')

    pb_headers = ['Date', 'Venue', 'Title of the\nProgram',
                  'No. of\nParticipants', 'Name of Resource\nPerson/s']
    pb_cols = [w * 0.12, w * 0.18, w * 0.28, w * 0.12, w * 0.30]

    pb_rows_filled = [(ws_dates[i] if i < len(ws_dates) else '').strip() or
                      (ws_titles[i] if i < len(ws_titles) else '').strip()
                      for i in range(len(ws_dates))]
    has_pb_data = any(pb_rows_filled)

    if has_pb_data:
        if not has_pa_data:
            elements.append(section_header('Section I: Quality Assurance Initiatives'))
            elements.append(Spacer(1, 4))
        elements.append(Paragraph(
            '(b) Workshops/Seminars/Training Programs organised by the IQAC coordinator (If any)',
            make_style('partb', size=8, bold=True, space_before=6, space_after=4)))
        pb_data = [[Paragraph(h, make_style(f'pbh{i}', size=7.5, bold=True, space_after=0, text_color=accent)) for i, h in enumerate(pb_headers)]]
        for i in range(len(ws_dates)):
            if not pb_rows_filled[i]:
                continue
            
            ws_title = ws_titles[i] if i < len(ws_titles) else ''
            pb_data.append([
                Paragraph(format_date(ws_dates[i]) if i < len(ws_dates) else '', small),
                Paragraph(esc(ws_venues[i]) if i < len(ws_venues) else '', small),
                Paragraph(esc(ws_title), small),
                Paragraph(esc(ws_parts[i]) if i < len(ws_parts) else '', small),
                Paragraph(esc(ws_res[i]) if i < len(ws_res) else '', small),
            ])
        pb_table = Table(pb_data, colWidths=pb_cols, repeatRows=1)
        pb_table.setStyle(table_style())
        elements.append(pb_table)

    # Report description field removed — no additional report text will be included here

    # ── Section II ──────────────────────────────────────────────────────────
    plans = [p.strip() for p in form_data.getlist('plan[]') if p.strip()]
    if plans:
        elements.append(Spacer(1, 8))
        elements.append(section_header('Section II: Plans for Next Month'))
        elements.append(Spacer(1, 4))
        plan_rows = []
        for i, p in enumerate(plans, 1):
            plan_rows.append([Paragraph(f'{i}.', make_style(f'pn{i}', size=9, space_after=0)),
                              Paragraph(esc(p), make_style(f'pt{i}', size=9, space_after=0))])

        plan_table = Table(plan_rows, colWidths=[0.6 * cm, usable_width - 0.6 * cm])
        plan_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.black),
        ]))
        elements.append(plan_table)

    # ── Signature Footer ────────────────────────────────────────────────────
    elements.append(Spacer(1, 16))

    coord_sig = esc(form_data.get('sig_coordinator_name', ''))
    dean_rem = esc(form_data.get('sig_dean_remarks', ''))
    dir_rem = esc(form_data.get('sig_director_remarks', ''))
    footer_date = esc(form_data.get('footer_date', ''))

    _sc = [0]
    def sig_cell(label, value, show_remarks_lines=False):
        _sc[0] += 1
        n = _sc[0]
        line = '_' * 32
        items = [
            Paragraph(label, make_style(f'sigh{n}', size=8, bold=True, space_after=6)),
        ]
        if show_remarks_lines:
            # Empty space matching coordinator name height, then signature line
            items.append(Spacer(1, 20))
            items.append(Paragraph(line, make_style(f'sigln{n}', size=8, space_after=4)))
        else:
            if value:
                items.append(Paragraph(esc(value), make_style(f'sigval{n}', size=9, space_after=4)))
            items.append(Spacer(1, 4))
            items.append(Paragraph(line, make_style(f'sigln{n}', size=8, space_after=4)))
        items.append(Paragraph('(Signature)', make_style(f'sigs{n}', size=7, italic=True, space_after=0)))
        return items

    third = usable_width / 3
    sig_data = [
        [sig_cell('Name & Signature of\nIQAC Coordinator', coord_sig, show_remarks_lines=False),
         sig_cell('Remarks & Signature of\nDean', dean_rem, show_remarks_lines=True),
         sig_cell('Remarks & Signature of\nDirector IQAC', dir_rem, show_remarks_lines=True)],
    ]
    sig_table = Table(sig_data, colWidths=[third, third, third])
    sig_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(sig_table)
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f'Date: {format_date(footer_date)}', make_style('datetext', size=9)))

    doc.build(elements)
    buffer.seek(0)
    return buffer


# ============================================================================
# AQAR-ALIGNED IQAC COORDINATOR REPORT
# ============================================================================

@pdf_bp.route("/iqac_coordinator_report/download", methods=["POST"])
def iqac_coordinator_report_download():
    if "username" not in session:
        return redirect("/login")

    username = session["username"]
    conn = get_db_connection()
    cursor = get_cursor(conn)

    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cursor.fetchone()

    if not user or user["role"].lower() not in ("school iqac coordinator", "campus iqac coordinator"):
        conn.close()
        flash("Access denied.", "danger")
        return redirect("/login")

    if not REPORTLAB_AVAILABLE:
        conn.close()
        flash("PDF generation library (reportlab) is not installed on the server.", "danger")
        return redirect("/iqac_monthly_report")

    reporting_month = request.form.get("reporting_month", "report")

    # Check if report is locked
    cursor.execute("""
        SELECT status FROM signed_reports 
        WHERE username=%s AND reporting_month=%s
    """, (username, reporting_month))
    signed_row = cursor.fetchone()
    if signed_row and signed_row["status"] in ('pending_upload', 'uploaded', 'reviewed'):
        conn.close()
        flash("This report is locked because the PDF has been generated/submitted. No modifications are allowed.", "danger")
        return redirect("/iqac_dashboard")

    # ── Auto-save draft on download ──
    import json
    form_data_obj = {}
    for key in request.form.keys():
        if key.endswith('[]'):
            form_data_obj[key] = request.form.getlist(key)
        else:
            form_data_obj[key] = request.form.get(key)

    sort_list_fields(form_data_obj, "aqar_coordinator")

    aqar_emails_env = os.getenv("AQAR_COORDINATOR_EMAILS", "")
    aqar_emails = [e.strip().lower() for e in aqar_emails_env.split(",") if e.strip()]
    email = (user.get("email") or "").strip().lower()
    report_type = "aqar_coordinator" if email in aqar_emails else "standard"

    try:
        cursor.execute("""
            INSERT INTO report_drafts (username, report_type, reporting_month, form_data, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (username, report_type, reporting_month)
            DO UPDATE SET form_data = EXCLUDED.form_data, updated_at = CURRENT_TIMESTAMP
        """, (username, report_type, reporting_month, json.dumps(form_data_obj)))

        # Insert/update signed_reports status = 'pending_upload'
        cursor.execute("""
            SELECT status FROM signed_reports 
            WHERE username=%s AND reporting_month=%s
        """, (username, reporting_month))
        existing_report = cursor.fetchone()
        
        if not existing_report:
            cursor.execute("""
                INSERT INTO signed_reports (username, reporting_month, status)
                VALUES (%s, %s, 'pending_upload')
            """, (username, reporting_month))
        else:
            cursor.execute("""
                UPDATE signed_reports 
                SET status = 'pending_upload', remarks = NULL, uploaded_file_path = NULL
                WHERE username = %s AND reporting_month = %s
            """, (username, reporting_month))
            
        conn.commit()
    except Exception as e:
        print("Error saving draft/signed_reports:", str(e))
        conn.rollback()

    # Read AQAR coordinator names from env
    aqar_names_env = os.getenv("AQAR_COORDINATOR_NAMES", "")
    aqar_names = [n.strip() for n in aqar_names_env.split(",") if n.strip()] if aqar_names_env else []

    # Construct MultiDict from sorted form_data_obj to pass to _generate_aqar_coordinator_pdf
    sorted_multi_form = MultiDict()
    for k, v in form_data_obj.items():
        if isinstance(v, list):
            for val in v:
                sorted_multi_form.add(k, val)
        else:
            sorted_multi_form.add(k, v)

    try:
        pdf_buffer = _generate_aqar_coordinator_pdf(sorted_multi_form, aqar_names)
    except Exception as e:
        print(f"PDF generation error: {e}")
        conn.close()
        flash("PDF generation failed. Please try again.", "danger")
        return redirect("/iqac_coordinator_report")
    conn.close()

    full_name = (user.get("full_name") or username).strip()
    try:
        month_label = datetime.strptime(reporting_month, "%Y-%m").strftime("%B")
    except Exception:
        month_label = reporting_month
    safe_name = "".join(c if c.isalnum() or c in (' ', '-') else '' for c in full_name).strip()
    filename = f"{safe_name} {month_label} IQAC Report.pdf"

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


def _generate_aqar_coordinator_pdf(form_data, aqar_names=None):
    """Generate the AQAR-Aligned IQAC Coordinator Report PDF."""
    buffer = BytesIO()

    usable_width = A4[0] - 4 * cm  # 2cm margins each side

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=0.5 * cm,
        bottomMargin=2 * cm
    )

    styles = getSampleStyleSheet()

    accent      = colors.HexColor('#1F497D')
    tbl_header  = colors.HexColor('#BDD7EE')
    light_blue  = colors.HexColor('#BDD7EE')

    def make_style(name, size=9, bold=False, align=TA_LEFT, space_before=0, space_after=4, italic=False, text_color=None):
        fname = 'Times-Roman'
        if bold and italic:
            fname = 'Times-BoldItalic'
        elif bold:
            fname = 'Times-Bold'
        elif italic:
            fname = 'Times-Italic'
        kwargs = dict(parent=styles['Normal'], fontSize=size, fontName=fname,
                      alignment=align, spaceBefore=space_before, spaceAfter=space_after)
        if text_color:
            kwargs['textColor'] = text_color
        return ParagraphStyle(name, **kwargs)

    small = make_style('aqar_small', size=7.5)

    def format_date(d_str):
        if not d_str:
            return ''
        try:
            return datetime.strptime(d_str.strip(), '%Y-%m-%d').strftime('%d-%m-%Y')
        except Exception:
            return d_str

    _sh_counter = [0]
    def section_header(text):
        _sh_counter[0] += 1
        t = Table([[Paragraph(text, make_style(f'aqar_sh_{_sh_counter[0]}', size=10, bold=True,
                                               space_after=0, align=TA_LEFT, space_before=0,
                                               text_color=accent))]], colWidths=[usable_width])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), tbl_header),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
        ]))
        return t

    def table_style(has_header=True):
        ts = [
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
            ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]
        if has_header:
            ts += [
                ('BACKGROUND', (0, 0), (-1, 0), tbl_header),
                ('TEXTCOLOR', (0, 0), (-1, 0), accent),
                ('FONTNAME', (0, 0), (-1, 0), 'Times-Bold'),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ]
        return TableStyle(ts)

    elements = []

    # ── Logo ──
    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'christ_logo.png')
    if os.path.exists(logo_path):
        logo_width = 5.9 * cm
        logo_height = 2.0 * cm
        logo_image = RLImage(logo_path, width=logo_width, height=logo_height)
        logo_table = Table([['', logo_image]], colWidths=[usable_width - logo_width, logo_width])
        logo_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))
        elements.append(logo_table)
        elements.append(Spacer(1, 14))

    # ── Title ──
    elements.append(Paragraph('INTERNAL QUALITY ASSURANCE CELL (IQAC)', make_style('aqar_h1', size=13, bold=True, align=TA_CENTER, space_after=4)))
    elements.append(Paragraph('Monthly Work Done Report', make_style('aqar_h2', size=10, bold=True, align=TA_CENTER, space_after=4)))
    elements.append(Paragraph('(AQAR | NAAC | Rankings/Awards | Quality Assurance Activities)', make_style('aqar_h3', size=8, align=TA_CENTER, space_after=4, italic=True)))

    elements.append(HRFlowable(width=usable_width, thickness=2, color=accent, spaceAfter=10))

    # ── Particulars Table ──
    coord_name = esc(form_data.get('coordinator_name', ''))
    school = esc(form_data.get('school_campus', ''))
    rep_month_raw = form_data.get('reporting_month', '')
    try:
        rep_month_display = datetime.strptime(rep_month_raw, '%Y-%m').strftime('%m-%Y')
    except Exception:
        rep_month_display = rep_month_raw

    resp_areas_val = esc(form_data.get('responsibility_areas', ''))
    if not resp_areas_val:
        resp_areas_val = 'AQAR / NAAC / Rankings/Awards / Audits / Documentation / Others'

    w = usable_width
    particulars_data = [
        [Paragraph('<b>Particulars</b>', make_style('aqar_p_lbl', size=9, bold=True, space_after=0)),
         Paragraph('<b>Details</b>', make_style('aqar_p_val', size=9, bold=True, space_after=0))],
        [Paragraph('Name of IQAC Coordinator', make_style('aqar_p1', size=9, space_after=0)),
         Paragraph(coord_name, make_style('aqar_pv1', size=9, space_after=0))],
        [Paragraph('School/Campus', make_style('aqar_p2', size=9, space_after=0)),
         Paragraph(school, make_style('aqar_pv2', size=9, space_after=0))],
        [Paragraph('Reporting Month', make_style('aqar_p3', size=9, space_after=0)),
         Paragraph(rep_month_display, make_style('aqar_pv3', size=9, space_after=0))],
        [Paragraph('Responsibility Area(s)', make_style('aqar_p4', size=9, space_after=0)),
         Paragraph(resp_areas_val, make_style('aqar_pv4', size=9, space_after=0))],
    ]
    particulars_table = Table(particulars_data, colWidths=[w * 0.35, w * 0.65])
    particulars_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#AAAAAA')),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
        ('BACKGROUND', (0, 0), (-1, 0), tbl_header),
        ('BACKGROUND', (0, 1), (0, -1), light_blue),
        ('TEXTCOLOR', (0, 0), (-1, 0), accent),
        ('FONTNAME', (0, 0), (-1, 0), 'Times-Bold'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(particulars_table)
    elements.append(Spacer(1, 8))

    # ── Section 1: Activities Undertaken ──
    act_dates = form_data.getlist('act_date[]')
    act_tasks = form_data.getlist('act_task[]')
    act_areas = form_data.getlist('act_area[]')
    act_area_others = form_data.getlist('act_area_other[]')
    act_stakeholders = form_data.getlist('act_stakeholders[]')
    act_outcomes = form_data.getlist('act_outcome[]')
    act_statuses = form_data.getlist('act_status[]')

    # Build area text (use "Other" custom text if applicable)
    def get_area_text(area, area_other, idx):
        a = area[idx] if idx < len(area) else ''
        if a == 'Others' and idx < len(area_other) and area_other[idx].strip():
            return area_other[idx].strip()
        return a

    s1_headers = ['Date', 'Activity/Task\nUndertaken', 'Related Area', 'Dept/Stakeholders\nInvolved', 'Outcome/\nProgress Made', 'Status']
    s1_cols = [w*0.10, w*0.22, w*0.13, w*0.20, w*0.20, w*0.15]

    s1_rows_filled = [(act_dates[i] if i < len(act_dates) else '').strip() or
                      (act_tasks[i] if i < len(act_tasks) else '').strip()
                      for i in range(len(act_dates))]
    has_s1_data = any(s1_rows_filled)

    if has_s1_data:
        elements.append(section_header('1. Activities Undertaken During the Month'))
        elements.append(Spacer(1, 4))
        s1_data = [[Paragraph(h, make_style(f'aqar_s1h{i}', size=7.5, bold=True, space_after=0, text_color=accent)) for i, h in enumerate(s1_headers)]]
        for i in range(len(act_dates)):
            if not s1_rows_filled[i]:
                continue
            area_text = get_area_text(act_areas, act_area_others, i)
            s1_data.append([
                Paragraph(format_date(act_dates[i]) if i < len(act_dates) else '', small),
                Paragraph(esc(act_tasks[i]) if i < len(act_tasks) else '', small),
                Paragraph(esc(area_text), small),
                Paragraph(esc(act_stakeholders[i]) if i < len(act_stakeholders) else '', small),
                Paragraph(esc(act_outcomes[i]) if i < len(act_outcomes) else '', small),
                Paragraph(esc(act_statuses[i]) if i < len(act_statuses) else '', small),
            ])
        s1_table = Table(s1_data, colWidths=s1_cols, repeatRows=1)
        s1_table.setStyle(table_style())
        elements.append(s1_table)
        elements.append(Spacer(1, 8))

    # ── Section 2: Meetings, Workshops & Training ──
    meet_dates = form_data.getlist('meet_date[]')
    meet_programmes = form_data.getlist('meet_programme[]')
    meet_roles = form_data.getlist('meet_role[]')
    meet_outcomes = form_data.getlist('meet_outcome[]')

    s2_headers = ['Date', 'Programme / Meeting', 'Role\n(Organised/Coordinated/Attended/Resource Person)', 'Key Outcome']
    s2_cols = [w*0.12, w*0.28, w*0.28, w*0.32]

    s2_rows_filled = [(meet_dates[i] if i < len(meet_dates) else '').strip() or
                      (meet_programmes[i] if i < len(meet_programmes) else '').strip()
                      for i in range(len(meet_dates))]
    has_s2_data = any(s2_rows_filled)

    if has_s2_data:
        elements.append(section_header('2. Meetings, Workshops & Training Programmes Attended/Organised'))
        elements.append(Spacer(1, 4))
        s2_data = [[Paragraph(h, make_style(f'aqar_s2h{i}', size=7.5, bold=True, space_after=0, text_color=accent)) for i, h in enumerate(s2_headers)]]
        for i in range(len(meet_dates)):
            if not s2_rows_filled[i]:
                continue
            s2_data.append([
                Paragraph(format_date(meet_dates[i]) if i < len(meet_dates) else '', small),
                Paragraph(esc(meet_programmes[i]) if i < len(meet_programmes) else '', small),
                Paragraph(esc(meet_roles[i]) if i < len(meet_roles) else '', small),
                Paragraph(esc(meet_outcomes[i]) if i < len(meet_outcomes) else '', small),
            ])
        s2_table = Table(s2_data, colWidths=s2_cols, repeatRows=1)
        s2_table.setStyle(table_style())
        elements.append(s2_table)
        elements.append(Spacer(1, 8))

    # ── Section 3: Key Achievements ──
    achievements = [a.strip() for a in form_data.getlist('achievement[]') if a.strip()]

    if achievements:
        elements.append(section_header('3. Key Achievements During the Month'))
        elements.append(Spacer(1, 4))
        for i, a in enumerate(achievements, 1):
            elements.append(Paragraph(f'{i}. {esc(a)}', make_style(f'aqar_ach{i}', size=9, space_after=3)))
        elements.append(Spacer(1, 8))

    # ── Section 4: Challenges / Issues Faced ──
    challenges = [c.strip() for c in form_data.getlist('challenge[]') if c.strip()]

    if challenges:
        elements.append(section_header('4. Challenges / Issues Faced'))
        elements.append(Spacer(1, 4))
        for i, c in enumerate(challenges, 1):
            elements.append(Paragraph(f'{i}. {esc(c)}', make_style(f'aqar_ch{i}', size=9, space_after=3)))

    # ── Section 5: Action Plan for Next Month ──
    plan_activities = form_data.getlist('plan_activity[]')
    plan_areas = form_data.getlist('plan_area[]')
    plan_area_others = form_data.getlist('plan_area_other[]')
    plan_outcomes = form_data.getlist('plan_outcome[]')

    s5_headers = ['Planned Activity', 'Related Area', 'Expected Outcome']
    s5_cols = [w*0.40, w*0.25, w*0.35]

    s5_rows_filled = [(plan_activities[i] if i < len(plan_activities) else '').strip() or
                      (plan_areas[i] if i < len(plan_areas) else '').strip()
                      for i in range(len(plan_activities))]
    has_s5_data = any(s5_rows_filled)

    if has_s5_data:
        elements.append(section_header('5. Action Plan for Next Month'))
        elements.append(Spacer(1, 4))
        s5_data = [[Paragraph(h, make_style(f'aqar_s5h{i}', size=7.5, bold=True, space_after=0, text_color=accent)) for i, h in enumerate(s5_headers)]]
        for i in range(len(plan_activities)):
            if not s5_rows_filled[i]:
                continue
            area_text = get_area_text(plan_areas, plan_area_others, i)
            s5_data.append([
                Paragraph(esc(plan_activities[i]) if i < len(plan_activities) else '', small),
                Paragraph(esc(area_text), small),
                Paragraph(esc(plan_outcomes[i]) if i < len(plan_outcomes) else '', small),
            ])
        s5_table = Table(s5_data, colWidths=s5_cols, repeatRows=1)
        s5_table.setStyle(table_style())
        elements.append(s5_table)

    # ── Signature Section ──
    coord_sig = esc(form_data.get('sig_coordinator_name', ''))
    footer_date = form_data.get('footer_date', '')
    dir_rem = esc(form_data.get('sig_director_remarks', ''))

    sig_block = [
        Spacer(1, 20),
        Paragraph(f'<b>Name &amp; Signature of IQAC Coordinator:</b>  {coord_sig}   {"_" * 25}', make_style('aqar_sig1', size=9, space_after=12)),
        Paragraph(f'<b>Date:</b>  {format_date(footer_date)}', make_style('aqar_sig2', size=9, space_after=12)),
        Paragraph(f'<b>Remarks of Director, IQAC:</b>  {dir_rem or ""}   {"_" * 25}', make_style('aqar_sig3', size=9, space_after=0)),
    ]
    elements.append(KeepTogether(sig_block))

    doc.build(elements)
    buffer.seek(0)
    return buffer
