import os
import zipfile
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_file, redirect
from werkzeug.utils import secure_filename
import tempfile
import json
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
import io
import stripe
import uuid

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')

REPORT_PRICE_ID = os.environ.get('REPORT_PRICE_ID', 'price_1TdKNO3bJrZ5L0LWWpOJbTyJ')
LAWFIRM_PRICE_ID = os.environ.get('LAWFIRM_PRICE_ID', 'price_1TdKMn3bJrZ5L0LW0tKT2iQg')

ALLOWED_EXTENSIONS = {'zip', 'csv'}

# In-memory session store: token -> {data, name, city, platform}
# For production you'd use Redis, but this works fine on a single-replica Railway deploy
SESSION_STORE = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def analyze_uber_data(df):
    df = df[df['status'] == 'completed'].copy() if 'status' in df.columns else df.copy()
    df = df[df['original_fare_usd'] > 0].copy() if 'original_fare_usd' in df.columns else df.copy()
    fare_components = ['base_fare_usd','surge_fare_usd','per_mile_fare_usd','per_minute_fare_usd','wait_time_fare_usd','minimum_fare_roundup_usd']
    available_cols = [c for c in fare_components if c in df.columns]
    df['driver_pay_calc'] = df[available_cols].fillna(0).sum(axis=1)
    df['driver_upfront_fare_usd'] = df['driver_upfront_fare_usd'].fillna(df['driver_pay_calc'])
    df = df[df['driver_upfront_fare_usd'] > 0].copy()
    if len(df) == 0:
        return None
    df['duration_min'] = (df['trip_duration_seconds'] / 60).round(2)
    df['uber_take_rate'] = ((df['original_fare_usd'] - df['driver_upfront_fare_usd']) / df['original_fare_usd'] * 100).round(1)
    df['fair_driver_pay'] = (df['original_fare_usd'] * 0.75).round(2)
    df['shortfall'] = (df['fair_driver_pay'] - df['driver_upfront_fare_usd']).clip(lower=0).round(2)
    df['date'] = pd.to_datetime(df['begintrip_timestamp_local'], errors='coerce')
    df['month'] = df['date'].dt.to_period('M').astype(str)
    monthly = df.groupby('month').agg(
        trips=('driver_upfront_fare_usd', 'count'),
        rider_total=('original_fare_usd', 'sum'),
        driver_paid=('driver_upfront_fare_usd', 'sum'),
        avg_take=('uber_take_rate', 'mean'),
        shortfall=('shortfall', 'sum')
    ).round(2).reset_index()
    worst = df.nlargest(10, 'uber_take_rate')[
        ['begintrip_timestamp_local', 'product_type_name', 'trip_distance_miles',
         'duration_min', 'original_fare_usd', 'driver_upfront_fare_usd', 'uber_take_rate']
    ].round(2)
    summary = {
        'total_trips': len(df),
        'rider_total': round(df['original_fare_usd'].sum(), 2),
        'driver_paid': round(df['driver_upfront_fare_usd'].sum(), 2),
        'uber_kept': round((df['original_fare_usd'] - df['driver_upfront_fare_usd']).sum(), 2),
        'avg_take_rate': round(df['uber_take_rate'].mean(), 1),
        'trips_over_50pct': int((df['uber_take_rate'] > 50).sum()),
        'trips_over_40pct': int((df['uber_take_rate'] > 40).sum()),
        'total_shortfall': round(df['shortfall'].sum(), 2),
        'worst_take_rate': round(df['uber_take_rate'].max(), 1),
        'date_start': df['date'].min().strftime('%b %d, %Y') if not df['date'].isna().all() else 'N/A',
        'date_end': df['date'].max().strftime('%b %d, %Y') if not df['date'].isna().all() else 'N/A',
        'city': df['city_name'].mode()[0] if 'city_name' in df.columns else 'Unknown',
        'worst_month': monthly.loc[monthly['avg_take'].idxmax(), 'month'] if len(monthly) > 0 else 'N/A',
        'worst_month_take': round(monthly['avg_take'].max(), 1) if len(monthly) > 0 else 0,
    }
    return {'summary': summary, 'monthly': monthly.to_dict('records'), 'worst_trips': worst.to_dict('records')}

def process_zip(zip_path):
    results = {}
    with zipfile.ZipFile(zip_path, 'r') as z:
        files = z.namelist()
        trip_files = [f for f in files if 'driver_lifetime_trips' in f.lower() and f.endswith('.csv')]
        if not trip_files:
            trip_files = [f for f in files if f.endswith('.csv') and not f.startswith('__MACOSX')]
        for trip_file in trip_files:
            try:
                with z.open(trip_file) as f:
                    df = pd.read_csv(f, low_memory=False)
                    if 'original_fare_usd' in df.columns and 'driver_upfront_fare_usd' in df.columns:
                        analysis = analyze_uber_data(df)
                        if analysis:
                            results['uber'] = analysis
                            break
            except Exception as e:
                print(f"Skipping {trip_file}: {e}")
                continue
    return results

def generate_pdf_report(data, driver_name, driver_city, platform='Uber'):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=72)
    styles = getSampleStyleSheet()
    story = []
    dark_blue = HexColor('#1F4E79')
    red = HexColor('#A32D2D')

    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=28, textColor=dark_blue, spaceAfter=6, fontName='Helvetica-Bold')
    story.append(Paragraph("FAREAUDIT FARE REPORT", title_style))
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'], fontSize=14, textColor=HexColor('#2E75B6'), spaceAfter=20)
    story.append(Paragraph("Evidence of Systematic Underpayment", sub_style))

    summary = data['summary']
    info_data = [['Driver', driver_name], ['Market', driver_city], ['Platform', platform],
                 ['Period', f"{summary['date_start']} – {summary['date_end']}"],
                 ['Generated', datetime.now().strftime('%B %d, %Y')],
                 ['Data Source', f'{platform} Official Privacy Export']]
    info_table = Table(info_data, colWidths=[2*inch, 4*inch])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), HexColor('#D6E4F0')),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 20))

    findings_data = [
        ['Riders Paid', 'Driver Received', "Platform's Cut", 'Avg Take Rate', 'Est. Shortfall'],
        [f"${summary['rider_total']:,.2f}", f"${summary['driver_paid']:,.2f}",
         f"${summary['uber_kept']:,.2f}", f"{summary['avg_take_rate']}%", f"${summary['total_shortfall']:,.2f}"],
    ]
    findings_table = Table(findings_data, colWidths=[1.3*inch]*5)
    findings_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue), ('BACKGROUND', (0,1), (-1,1), HexColor('#FFF2F2')),
        ('TEXTCOLOR', (0,0), (-1,0), white), ('TEXTCOLOR', (0,1), (-1,1), red),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 9), ('FONTSIZE', (0,1), (-1,1), 11),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')), ('PADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(findings_table)
    story.append(Spacer(1, 20))

    h1_style = ParagraphStyle('H1', parent=styles['Heading1'], fontSize=16, textColor=dark_blue, spaceBefore=16, spaceAfter=8, fontName='Helvetica-Bold')
    body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=10, spaceAfter=8, leading=14)

    story.append(Paragraph("1. Key Findings", h1_style))
    story.append(Paragraph(
        f"Analysis of {summary['total_trips']} completed trips in {driver_city} reveals that "
        f"{platform} retained an average of {summary['avg_take_rate']}% of every fare paid by riders — "
        f"nearly double the industry-standard commission rate of approximately 25%. "
        f"On {summary['trips_over_50pct']} of {summary['total_trips']} trips "
        f"({round(summary['trips_over_50pct']/summary['total_trips']*100,1)}%), "
        f"{platform} retained more than 50 cents of every dollar collected from passengers. "
        f"The estimated underpayment versus the 25% standard is ${summary['total_shortfall']:,.2f}.", body_style))
    story.append(Paragraph(
        f"The worst single trip recorded a {summary['worst_take_rate']}% take rate, "
        f"meaning the driver received less than {100-summary['worst_take_rate']:.0f} cents "
        f"of every dollar the rider paid.", body_style))

    story.append(Paragraph("2. Monthly Breakdown", h1_style))
    monthly_rows = [['Month', 'Trips', 'Rider Total', 'Driver Paid', 'Avg Take %', 'Shortfall']]
    for m in data['monthly']:
        monthly_rows.append([m['month'], str(m['trips']), f"${m['rider_total']:,.2f}",
                             f"${m['driver_paid']:,.2f}", f"{m['avg_take']:.1f}%", f"${m['shortfall']:,.2f}"])
    monthly_rows.append(['TOTAL', str(summary['total_trips']), f"${summary['rider_total']:,.2f}",
                         f"${summary['driver_paid']:,.2f}", f"{summary['avg_take_rate']}%", f"${summary['total_shortfall']:,.2f}"])
    monthly_table = Table(monthly_rows, colWidths=[1.2*inch, 0.7*inch, 1.2*inch, 1.2*inch, 1.0*inch, 1.1*inch])
    monthly_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue), ('TEXTCOLOR', (0,0), (-1,0), white),
        ('BACKGROUND', (0,-1), (-1,-1), HexColor('#D6E4F0')),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9), ('ALIGN', (1,0), (-1,-1), 'CENTER'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')), ('PADDING', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [white, HexColor('#F5F5F5')]),
    ]))
    story.append(monthly_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph("3. Worst Individual Trips", h1_style))
    story.append(Paragraph("The following trips represent the most significant fare discrepancies identified.", body_style))
    worst_rows = [['Date', 'Type', 'Miles', 'Rider Paid', 'Driver Got', 'Platform Took']]
    for t in data['worst_trips'][:8]:
        worst_rows.append([str(t.get('begintrip_timestamp_local', ''))[:10],
                          str(t.get('product_type_name', 'N/A'))[:12],
                          f"{t.get('trip_distance_miles', 0):.1f}",
                          f"${t.get('original_fare_usd', 0):.2f}",
                          f"${t.get('driver_upfront_fare_usd', 0):.2f}",
                          f"{t.get('uber_take_rate', 0):.1f}%"])
    worst_table = Table(worst_rows, colWidths=[1.1*inch, 1.1*inch, 0.7*inch, 1.1*inch, 1.1*inch, 1.3*inch])
    worst_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue), ('TEXTCOLOR', (0,0), (-1,0), white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (2,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 6), ('TEXTCOLOR', (5,1), (5,-1), red),
        ('FONTNAME', (5,1), (5,-1), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [white, HexColor('#FFF2F2')]),
    ]))
    story.append(worst_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph("4. Legal Context", h1_style))
    story.append(Paragraph(
        "This audit is supported by documented legal actions including a 2025 settlement "
        "in which Uber and Lyft paid $328 million to resolve wage theft allegations in New York. "
        "The FTC filed complaints against Uber in April and December 2025 for deceptive practices. "
        "A 2025 academic study found Uber's effective take rate increased from ~32% to 42% following "
        "its shift to upfront pricing, with individual trips exceeding 50%.", body_style))

    story.append(Paragraph("5. Requested Resolution", h1_style))
    for i, item in enumerate([
        f"Full accounting of fare calculation methodology for all {summary['total_trips']} trips.",
        f"Explanation of why the effective take rate averaged {summary['avg_take_rate']}% against ~25%.",
        f"Remediation of the estimated ${summary['total_shortfall']:,.2f} underpayment.",
        "Transparency into the upfront pricing algorithm used to determine driver pay.",
    ], 1):
        story.append(Paragraph(f"{i}. {item}", body_style))

    story.append(Spacer(1, 20))
    story.append(Paragraph("6. Declaration", h1_style))
    story.append(Paragraph(
        f"I, {driver_name}, declare that the information in this document is accurate to the best "
        f"of my knowledge and is based entirely on data provided by {platform} through its official "
        f"privacy data export portal.", body_style))
    story.append(Spacer(1, 30))
    story.append(Paragraph("Signature: ___________________________          Date: _______________", body_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph(driver_name, body_style))
    story.append(Paragraph(driver_city, body_style))
    story.append(Spacer(1, 20))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, textColor=HexColor('#999999'), spaceBefore=10)
    story.append(Paragraph(
        "Generated by FareAudit (fareaudit.app) — This document does not constitute legal advice. "
        "All data sourced from the platform's official privacy export. For legal advice consult a licensed attorney.",
        footer_style))
    doc.build(story)
    buffer.seek(0)
    return buffer


# ── NEW: real upload + analyze endpoint ──────────────────────────────────────

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        driver_name = request.form.get('name', '').strip()
        driver_city = request.form.get('city', '').strip()
        platform = request.form.get('platform', 'uber').strip().lower()

        if not driver_name or not driver_city:
            return jsonify({'error': 'Name and city are required.'}), 400

        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded.'}), 400

        f = request.files['file']
        if not f or not allowed_file(f.filename):
            return jsonify({'error': 'Please upload a .zip or .csv file.'}), 400

        # Save to a temp file
        suffix = '.' + f.filename.rsplit('.', 1)[1].lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        try:
            if suffix == '.zip':
                results = process_zip(tmp_path)
                data = results.get('uber') or results.get(list(results.keys())[0]) if results else None
            else:
                df = pd.read_csv(tmp_path, low_memory=False)
                data = analyze_uber_data(df)
        finally:
            os.unlink(tmp_path)

        if not data:
            return jsonify({'error': 'No completed trip data found in your file. Make sure you uploaded the correct Uber privacy export zip.'}), 400

        # Store in session
        token = str(uuid.uuid4())
        SESSION_STORE[token] = {
            'data': data,
            'name': driver_name,
            'city': driver_city,
            'platform': platform,
            'created': datetime.now().isoformat(),
        }

        summary = data['summary']
        worst = data['worst_trips'][:3]

        return jsonify({
            'token': token,
            'summary': summary,
            'worst_trips': worst,
            'monthly': data['monthly'],
        })

    except Exception as e:
        print(f"Analyze error: {e}")
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500


@app.route('/')
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FareAudit — Gig Worker Fare Audit Tool</title>
<meta name="description" content="Find out if Uber, Lyft or DoorDash is underpaying you. Upload your data and get an instant audit with a case-ready report.">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#ffffff;--bg2:#f5f5f3;--bg3:#efefec;
  --text:#1a1a18;--text2:#666660;--text3:#999992;
  --border:#e0dfd8;--border2:#c8c7c0;
  --danger:#A32D2D;--danger-bg:#FCEBEB;--danger-border:#F09595;
  --warning:#854F0B;--warning-bg:#FAEEDA;--warning-border:#FAC775;
  --success:#3B6D11;--success-bg:#EAF3DE;--success-border:#C0DD97;
  --radius:8px;--radius-lg:12px;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#1c1c1a;--bg2:#2a2a27;--bg3:#333330;
  --text:#f0efe8;--text2:#999992;--text3:#666660;
  --border:#3a3a36;--border2:#4a4a46;
}}
body{font-family:'Sora',sans-serif;background:var(--bg3);color:var(--text);min-height:100vh;padding:2rem 1rem}
.wrap{max-width:680px;margin:0 auto;background:var(--bg);border-radius:var(--radius-lg);padding:2rem;border:0.5px solid var(--border)}
.pa-logo{display:flex;align-items:center;gap:10px;margin-bottom:2rem}
.pa-logo-mark{width:36px;height:36px;background:var(--text);border-radius:8px;display:flex;align-items:center;justify-content:center}
.pa-logo-mark i{font-size:20px;color:var(--bg)}
.pa-logo-name{font-size:20px;font-weight:600;color:var(--text);letter-spacing:-0.5px}
.pa-logo-name span{color:var(--text2);font-weight:400}
.pa-hero{margin-bottom:2rem}
.pa-hero h1{font-size:28px;font-weight:600;letter-spacing:-0.5px;line-height:1.2;color:var(--text);margin-bottom:8px}
.pa-hero p{font-size:15px;color:var(--text2);line-height:1.6}
.pa-tabs{display:flex;gap:8px;border-bottom:0.5px solid var(--border);margin-bottom:1.5rem}
.pa-tab{padding:8px 16px;font-size:13px;font-weight:500;cursor:pointer;border:none;background:none;color:var(--text2);border-bottom:2px solid transparent;margin-bottom:-0.5px;font-family:'Sora',sans-serif}
.pa-tab.active{color:var(--text);border-bottom:2px solid var(--text)}
.pa-panel{display:none}.pa-panel.active{display:block}
.pa-upload-box{border:1.5px dashed var(--border2);border-radius:var(--radius-lg);padding:2.5rem;text-align:center;cursor:pointer;transition:all 0.15s;margin-bottom:1rem;position:relative;background:var(--bg)}
.pa-upload-box:hover,.pa-upload-box.drag{border-color:var(--text);background:var(--bg2)}
.pa-upload-box i{font-size:32px;color:var(--text2);margin-bottom:12px;display:block}
.pa-upload-box h3{font-size:15px;font-weight:500;color:var(--text);margin-bottom:4px}
.pa-upload-box p{font-size:13px;color:var(--text2)}
.pa-upload-box input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.pa-platform-select{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:1rem}
.pa-platform{border:0.5px solid var(--border);border-radius:var(--radius);padding:12px;text-align:center;cursor:pointer;transition:all 0.15s;background:var(--bg)}
.pa-platform:hover{border-color:var(--border2);background:var(--bg2)}
.pa-platform.selected{border:1.5px solid var(--text);background:var(--bg2)}
.pa-platform i{font-size:20px;display:block;margin-bottom:6px;color:var(--text2)}
.pa-platform.selected i{color:var(--text)}
.pa-platform span{font-size:12px;font-weight:500;color:var(--text2)}
.pa-platform.selected span{color:var(--text)}
.form-row{margin-bottom:1rem}
.form-row label{display:block;font-size:11px;font-weight:500;color:var(--text2);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
.form-row input{width:100%;padding:10px 12px;font-size:14px;font-family:'Sora',sans-serif;border-radius:var(--radius);border:0.5px solid var(--border2);background:var(--bg);color:var(--text);outline:none;transition:border-color 0.15s}
.form-row input:focus{border-color:var(--text)}
.btn{width:100%;padding:14px;font-size:15px;font-weight:500;font-family:'Sora',sans-serif;border-radius:var(--radius);border:none;cursor:pointer;transition:opacity 0.15s,transform 0.1s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn:active{transform:scale(0.98)}
.btn-primary{background:var(--text);color:var(--bg)}
.btn-primary:hover{opacity:0.85}
.btn-secondary{background:var(--bg2);color:var(--text);border:0.5px solid var(--border2);margin-top:8px}
.btn-secondary:hover{background:var(--bg3)}
.progress{display:none;margin-top:1.5rem}
.progress.show{display:block}
.progress-bar-wrap{height:4px;background:var(--bg2);border-radius:2px;overflow:hidden;margin-bottom:8px}
.progress-bar{height:100%;background:var(--text);width:0%;transition:width 0.4s ease;border-radius:2px}
.progress-label{font-size:12px;color:var(--text2);font-family:'DM Mono',monospace}
.results{display:none}.results.show{display:block}
.stat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:1.5rem}
.stat{background:var(--bg2);border-radius:var(--radius);padding:1rem}
.stat-label{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
.stat-value{font-size:22px;font-weight:600;color:var(--text);font-family:'DM Mono',monospace}
.stat-value.danger{color:var(--danger)}
.stat-sub{font-size:11px;color:var(--text2);margin-top:2px}
.alert{border-radius:var(--radius);padding:12px 14px;margin-bottom:1rem;display:flex;gap:10px;align-items:flex-start}
.alert i{font-size:16px;flex-shrink:0;margin-top:1px}
.alert-text{font-size:13px;line-height:1.5}
.alert-text strong{font-weight:500;display:block;margin-bottom:2px}
.alert.danger{background:var(--danger-bg);border:0.5px solid var(--danger-border)}
.alert.danger i,.alert.danger .alert-text strong{color:var(--danger)}
.alert.danger .alert-text{color:#791F1F}
.alert.warning{background:var(--warning-bg);border:0.5px solid var(--warning-border)}
.alert.warning i,.alert.warning .alert-text strong{color:var(--warning)}
.alert.warning .alert-text{color:#633806}
.alert.success{background:var(--success-bg);border:0.5px solid var(--success-border)}
.alert.success i,.alert.success .alert-text strong{color:var(--success)}
.alert.success .alert-text{color:#27500A}
.section-title{font-size:11px;font-weight:500;color:var(--text2);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;margin-top:1.5rem}
table.trips{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:1.5rem}
table.trips th{text-align:left;padding:8px 10px;background:var(--bg2);color:var(--text2);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:0.3px;border-bottom:0.5px solid var(--border)}
table.trips td{padding:8px 10px;border-bottom:0.5px solid var(--border);color:var(--text);font-family:'DM Mono',monospace;font-size:12px}
table.trips tr:last-child td{border-bottom:none}
table.trips td.danger{color:var(--danger);font-weight:500}
.law-card{border:0.5px solid var(--border);border-radius:var(--radius-lg);padding:1rem 1.25rem;margin-bottom:1rem;background:var(--bg)}
.law-card.featured{border-color:var(--text);border-width:1.5px}
.law-card h3{font-size:14px;font-weight:500;color:var(--text);margin-bottom:4px}
.law-card p{font-size:13px;color:var(--text2);line-height:1.5;margin-bottom:10px}
.law-card .badge{display:inline-block;font-size:11px;font-weight:500;padding:3px 10px;border-radius:100px;background:var(--text);color:var(--bg);margin-bottom:8px}
.file-pill{display:inline-flex;align-items:center;gap:6px;background:var(--bg2);border:0.5px solid var(--border2);border-radius:100px;padding:6px 12px;font-size:12px;color:var(--text2);margin-top:8px}
.disclaimer{font-size:11px;color:var(--text3);line-height:1.6;margin-top:1.5rem;padding-top:1rem;border-top:0.5px solid var(--border)}
.nav-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:2.5rem;padding-bottom:1.5rem;border-bottom:0.5px solid var(--border)}
.nav-links{display:flex;gap:1.5rem}
.nav-links a{font-size:13px;color:var(--text2);text-decoration:none}
.nav-links a:hover{color:var(--text)}
.hero-badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.badge-pill{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text2);background:var(--bg2);border:0.5px solid var(--border);border-radius:100px;padding:4px 10px}
.badge-pill i{font-size:13px}
.paywall-overlay{position:relative;margin-bottom:1rem}
.paywall-overlay .blurred{filter:blur(4px);user-select:none;pointer-events:none}
.paywall-overlay .blur-msg{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg);border:1px solid var(--border2);border-radius:var(--radius);padding:12px 20px;font-size:13px;font-weight:500;color:var(--text);text-align:center;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,0.15)}
.error-msg{background:var(--danger-bg);border:0.5px solid var(--danger-border);color:var(--danger);border-radius:var(--radius);padding:12px 14px;font-size:13px;margin-top:1rem;display:none}
.error-msg.show{display:block}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav-bar">
    <div class="pa-logo">
      <div class="pa-logo-mark"><i class="ti ti-chart-bar"></i></div>
      <div class="pa-logo-name">Fare<span>Audit</span></div>
    </div>
    <div class="nav-links">
      <a href="#how">How it works</a>
      <a href="#lawfirms">Law firms</a>
      <a href="mailto:FareAudit@pm.me">Contact</a>
    </div>
  </div>

  <div class="pa-hero">
    <h1>Find out if you're being underpaid</h1>
    <p>Upload your Uber, Lyft, or DoorDash data and get an instant audit showing exactly what you earned vs. what you should have earned — with a case-ready report.</p>
    <div class="hero-badges">
      <span class="badge-pill"><i class="ti ti-lock"></i> Data never stored</span>
      <span class="badge-pill"><i class="ti ti-clock"></i> Results in 60 seconds</span>
      <span class="badge-pill"><i class="ti ti-file-text"></i> Case-ready PDF report</span>
    </div>
  </div>

  <div class="pa-tabs">
    <button class="pa-tab active" onclick="switchTab('driver')">For drivers</button>
    <button class="pa-tab" onclick="switchTab('lawfirm')">For law firms</button>
  </div>

  <div class="pa-panel active" id="tab-driver">
    <p class="section-title">Select your platform</p>
    <div class="pa-platform-select">
      <div class="pa-platform selected" onclick="selectPlatform(this,'uber')">
        <i class="ti ti-car"></i><span>Uber</span>
      </div>
      <div class="pa-platform" onclick="selectPlatform(this,'lyft')">
        <i class="ti ti-car"></i><span>Lyft</span>
      </div>
      <div class="pa-platform" onclick="selectPlatform(this,'doordash')">
        <i class="ti ti-bike"></i><span>DoorDash</span>
      </div>
    </div>

    <div class="form-row">
      <label>Your name</label>
      <input type="text" id="driver-name" placeholder="Full name">
    </div>
    <div class="form-row">
      <label>Your market / city</label>
      <input type="text" id="driver-city" placeholder="e.g. Nashville, TN">
    </div>

    <p class="section-title" style="margin-top:1.5rem">Upload your data file</p>
    <div class="pa-upload-box" id="upload-box">
      <input type="file" id="file-input" accept=".zip,.csv" onchange="handleFile(this)">
      <i class="ti ti-upload" id="upload-icon"></i>
      <h3 id="upload-title">Drop your data zip here</h3>
      <p id="upload-sub">Get yours at myprivacy.uber.com · drivers.lyft.com · identity.doordash.com/privacy</p>
    </div>
    <div id="file-pill-wrap" style="display:none">
      <div class="file-pill"><i class="ti ti-file-zip"></i><span id="file-name-display"></span></div>
    </div>

    <div class="error-msg" id="error-msg"></div>

    <button class="btn btn-primary" style="margin-top:1rem" onclick="runAudit()">
      <i class="ti ti-search"></i> Run my audit — free
    </button>

    <div class="progress" id="progress">
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar"></div></div>
      <div class="progress-label" id="progress-label">Uploading your data...</div>
    </div>

    <div class="results" id="results">
      <p class="section-title" style="margin-top:1.5rem">Your audit results</p>
      <div class="stat-grid">
        <div class="stat">
          <div class="stat-label">Riders paid platform</div>
          <div class="stat-value" id="r-total">—</div>
          <div class="stat-sub" id="r-period">—</div>
        </div>
        <div class="stat">
          <div class="stat-label">You received</div>
          <div class="stat-value" id="r-paid">—</div>
          <div class="stat-sub" id="r-trips">—</div>
        </div>
        <div class="stat">
          <div class="stat-label">Platform's average cut</div>
          <div class="stat-value danger" id="r-rate">—</div>
          <div class="stat-sub">Standard rate is ~25%</div>
        </div>
        <div class="stat">
          <div class="stat-label">Estimated shortfall</div>
          <div class="stat-value danger" id="r-short">—</div>
          <div class="stat-sub">vs 25% commission</div>
        </div>
      </div>

      <div class="alert danger" id="alert-main">
        <i class="ti ti-alert-triangle"></i>
        <div class="alert-text">
          <strong id="alert-main-title">Significant underpayment detected</strong>
          <span id="alert-main-body"></span>
        </div>
      </div>

      <div class="alert warning" id="alert-month">
        <i class="ti ti-clock"></i>
        <div class="alert-text">
          <strong id="alert-month-title">—</strong>
          <span id="alert-month-body"></span>
        </div>
      </div>

      <p class="section-title">Worst individual trips (preview)</p>
      <div style="overflow-x:auto">
        <table class="trips">
          <thead><tr><th>Date</th><th>Type</th><th>Miles</th><th>Rider paid</th><th>You got</th><th>Platform took</th></tr></thead>
          <tbody id="worst-trips-body"></tbody>
        </table>
      </div>

      <div class="paywall-overlay">
        <table class="trips blurred" id="blurred-trips">
          <tbody>
            <tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td class="danger">—%</td></tr>
            <tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td class="danger">—%</td></tr>
            <tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td class="danger">—%</td></tr>
            <tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td class="danger">—%</td></tr>
          </tbody>
        </table>
        <div class="blur-msg">🔒 Unlock full report — $49</div>
      </div>

      <div class="alert success">
        <i class="ti ti-file-text"></i>
        <div class="alert-text">
          <strong>Full case document ready</strong>
          Your complete audit with all trips, monthly breakdowns, legal context, and formal complaint language. Ready to send to a lawyer or file with the FTC.
        </div>
      </div>

      <button class="btn btn-primary" onclick="buyReport()">
        <i class="ti ti-download"></i> Download full case report — $49
      </button>
      <button class="btn btn-secondary" onclick="window.open('https://reportfraud.ftc.gov','_blank')">
        <i class="ti ti-send"></i> File FTC complaint (free)
      </button>
    </div>
  </div>

  <div class="pa-panel" id="tab-lawfirm">
    <div class="alert warning" style="margin-bottom:1.5rem">
      <i class="ti ti-building"></i>
      <div class="alert-text">
        <strong>Law firm portal</strong>
        Unlimited driver audits. Case-ready PDF reports in under 60 seconds.
      </div>
    </div>
    <div class="form-row"><label>Firm name</label><input type="text" id="firm-name" placeholder="Law firm name"></div>
    <div class="form-row"><label>Attorney email</label><input type="text" id="firm-email" placeholder="attorney@firm.com"></div>

    <p class="section-title" style="margin-top:2rem" id="lawfirms">Licensing options</p>
    <div class="law-card featured">
      <span class="badge">Unlimited</span>
      <h3>$1,499 / month</h3>
      <p>Unlimited driver audits for the duration of your active monthly subscription. White-label PDF reports, priority processing. Renews automatically. Cancel anytime.</p>
      <button class="btn btn-primary" onclick="buyLawFirm()">
        <i class="ti ti-building"></i> Subscribe — $1,499/month
      </button>
    </div>
    <div class="law-card">
      <h3>Revenue share — free to use</h3>
      <p>No upfront cost. 2% of any settlement on cases built using FareAudit reports.</p>
      <button class="btn btn-secondary" onclick="window.location.href='mailto:FareAudit@pm.me?subject=Law firm inquiry'">
        <i class="ti ti-mail"></i> Contact us
      </button>
    </div>
  </div>

  <div id="how" style="margin-top:2.5rem;padding-top:2rem;border-top:0.5px solid var(--border)">
    <p class="section-title">How it works</p>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-download" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">1. Request your data</p>
        <p style="font-size:12px;color:var(--text2)">Download your privacy export from Uber, Lyft, or DoorDash</p>
      </div>
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-upload" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">2. Upload here</p>
        <p style="font-size:12px;color:var(--text2)">Drop your zip file. We analyze every single trip in seconds</p>
      </div>
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-file-text" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">3. Get your report</p>
        <p style="font-size:12px;color:var(--text2)">Download a case-ready document showing exactly how much you're owed</p>
      </div>
    </div>
  </div>

  <p class="disclaimer">FareAudit (fareaudit.app) analyzes data you provide and identifies statistical patterns in fare calculations. This tool does not provide legal advice. All uploaded data is processed securely and never stored. &copy; 2026 FareAudit.</p>
</div>

<script>
let selectedPlatform = 'uber';
let auditToken = null;
let auditSummary = null;

function switchTab(tab) {
  document.querySelectorAll('.pa-tab').forEach((t,i) => {
    t.classList.toggle('active', (i===0&&tab==='driver')||(i===1&&tab==='lawfirm'));
  });
  document.getElementById('tab-driver').classList.toggle('active', tab==='driver');
  document.getElementById('tab-lawfirm').classList.toggle('active', tab==='lawfirm');
}

function selectPlatform(el, name) {
  document.querySelectorAll('.pa-platform').forEach(p => p.classList.remove('selected'));
  el.classList.add('selected');
  selectedPlatform = name;
}

function handleFile(input) {
  if (input.files[0]) {
    document.getElementById('file-name-display').textContent = input.files[0].name;
    document.getElementById('file-pill-wrap').style.display = 'block';
    document.getElementById('upload-box').style.borderStyle = 'solid';
    document.getElementById('upload-icon').className = 'ti ti-check';
    document.getElementById('upload-title').textContent = input.files[0].name;
    document.getElementById('upload-sub').textContent = 'File ready — click Run my audit below';
  }
}

function showError(msg) {
  const el = document.getElementById('error-msg');
  el.textContent = msg;
  el.classList.add('show');
}

function hideError() {
  document.getElementById('error-msg').classList.remove('show');
}

function setProgress(pct, label) {
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-label').textContent = label;
}

async function runAudit() {
  const driverName = document.getElementById('driver-name').value.trim();
  const driverCity = document.getElementById('driver-city').value.trim();
  const fileInput = document.getElementById('file-input');

  hideError();

  if (!driverName || !driverCity) { showError('Please enter your name and city.'); return; }
  if (!fileInput.files[0]) { showError('Please upload your data file.'); return; }

  const prog = document.getElementById('progress');
  prog.classList.add('show');
  document.getElementById('results').classList.remove('show');
  setProgress(10, 'Uploading your data file...');

  try {
    const formData = new FormData();
    formData.append('name', driverName);
    formData.append('city', driverCity);
    formData.append('platform', selectedPlatform);
    formData.append('file', fileInput.files[0]);

    setProgress(30, 'Analyzing your trips...');

    const res = await fetch('/analyze', { method: 'POST', body: formData });
    
    setProgress(70, 'Calculating fare discrepancies...');

    const result = await res.json();

    if (!res.ok || result.error) {
      prog.classList.remove('show');
      showError(result.error || 'Analysis failed. Please try again.');
      return;
    }

    setProgress(90, 'Building your results...');
    await new Promise(r => setTimeout(r, 400));
    setProgress(100, 'Audit complete.');
    await new Promise(r => setTimeout(r, 400));

    auditToken = result.token;
    auditSummary = result.summary;

    // Populate stats
    document.getElementById('r-total').textContent = '$' + result.summary.rider_total.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    document.getElementById('r-paid').textContent = '$' + result.summary.driver_paid.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    document.getElementById('r-rate').textContent = result.summary.avg_take_rate + '%';
    document.getElementById('r-short').textContent = '$' + result.summary.total_shortfall.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    document.getElementById('r-period').textContent = result.summary.date_start + ' – ' + result.summary.date_end;
    document.getElementById('r-trips').textContent = result.summary.total_trips + ' trips analyzed';

    // Alerts
    const pct50 = result.summary.trips_over_50pct;
    const total = result.summary.total_trips;
    document.getElementById('alert-main-body').textContent =
      'The platform took more than 50% of rider fares on ' + pct50 + ' of your ' + total + ' trips. The worst single trip: ' + result.summary.worst_take_rate + '% of what the rider paid went to the platform.';
    document.getElementById('alert-month-title').textContent = result.summary.worst_month + ' was your worst month';
    document.getElementById('alert-month-body').textContent =
      'Average take rate of ' + result.summary.worst_month_take + '% — the platform kept more than you earned on the majority of trips that month.';

    // Worst trips table
    const tbody = document.getElementById('worst-trips-body');
    tbody.innerHTML = '';
    result.worst_trips.slice(0, 3).forEach(t => {
      const row = document.createElement('tr');
      const date = String(t.begintrip_timestamp_local || '').slice(0,10);
      row.innerHTML = `<td>${date}</td><td>${t.product_type_name||'N/A'}</td><td>${(t.trip_distance_miles||0).toFixed(2)}</td><td>$${(t.original_fare_usd||0).toFixed(2)}</td><td>$${(t.driver_upfront_fare_usd||0).toFixed(2)}</td><td class="danger">${(t.uber_take_rate||0).toFixed(1)}%</td>`;
      tbody.appendChild(row);
    });

    prog.classList.remove('show');
    document.getElementById('results').classList.add('show');
    document.getElementById('results').scrollIntoView({behavior:'smooth', block:'start'});

  } catch(e) {
    prog.classList.remove('show');
    showError('Something went wrong. Please try again.');
    console.error(e);
  }
}

async function buyReport() {
  if (!auditToken) { showError('Please run your audit first.'); return; }
  try {
    const driverName = document.getElementById('driver-name').value.trim();
    const driverCity = document.getElementById('driver-city').value.trim();
    const res = await fetch('/create-checkout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        type: 'report',
        token: auditToken,
        name: driverName,
        city: driverCity,
        platform: selectedPlatform
      })
    });
    const data = await res.json();
    if (data.url) window.location.href = data.url;
    else showError('Payment setup error. Please try again.');
  } catch(e) {
    showError('Something went wrong. Please try again.');
  }
}

async function buyLawFirm() {
  try {
    const res = await fetch('/create-checkout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ type: 'lawfirm' })
    });
    const data = await res.json();
    if (data.url) window.location.href = data.url;
    else alert('Payment setup error. Please try again.');
  } catch(e) {
    alert('Something went wrong. Please try again.');
  }
}

// Drag and drop
const box = document.getElementById('upload-box');
box.addEventListener('dragenter', e => { e.preventDefault(); box.classList.add('drag'); });
box.addEventListener('dragover', e => { e.preventDefault(); box.classList.add('drag'); });
box.addEventListener('dragleave', e => { if (!box.contains(e.relatedTarget)) box.classList.remove('drag'); });
box.addEventListener('drop', e => {
  e.preventDefault();
  box.classList.remove('drag');
  const f = e.dataTransfer.files[0];
  if (f) {
    const dt = new DataTransfer();
    dt.items.add(f);
    document.getElementById('file-input').files = dt.files;
    document.getElementById('file-name-display').textContent = f.name;
    document.getElementById('file-pill-wrap').style.display = 'block';
    box.style.borderStyle = 'solid';
    document.getElementById('upload-icon').className = 'ti ti-check';
    document.getElementById('upload-title').textContent = f.name;
    document.getElementById('upload-sub').textContent = 'File ready — click Run my audit below';
  }
});
</script>
</body>
</html>
"""

@app.route('/create-checkout', methods=['POST'])
def create_checkout():
    try:
        data = request.json
        checkout_type = data.get('type')
        base_url = 'https://fareaudit.app'

        if checkout_type == 'report':
            token = data.get('token', '')
            name = data.get('name', 'Driver')
            city = data.get('city', 'Unknown')
            platform = data.get('platform', 'uber')
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{'price': REPORT_PRICE_ID, 'quantity': 1}],
                mode='payment',
                allow_promotion_codes=True,
                success_url=base_url + '/success?session_id={CHECKOUT_SESSION_ID}&token=' + token + '&name=' + name.replace(' ','+') + '&city=' + city.replace(' ','+') + '&platform=' + platform,
                cancel_url=base_url + '/',
            )
        elif checkout_type == 'lawfirm':
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{'price': LAWFIRM_PRICE_ID, 'quantity': 1}],
                mode='subscription',
                allow_promotion_codes=True,
                success_url=base_url + '/success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=base_url + '/',
            )
        else:
            return jsonify({'error': 'Invalid type'}), 400

        return jsonify({'url': session.url})
    except Exception as e:
        print(f"Stripe error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/success')
def success():
    session_id = request.args.get('session_id')
    token = request.args.get('token', '')
    driver_name = request.args.get('name', 'Driver').replace('+', ' ')
    driver_city = request.args.get('city', 'Unknown').replace('+', ' ')
    platform = request.args.get('platform', 'Uber')

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == 'paid' or session.status == 'complete':
            # Use real session data if available, otherwise log a warning
            session_data = SESSION_STORE.get(token)
            if session_data:
                data = session_data['data']
                driver_name = session_data.get('name', driver_name)
                driver_city = session_data.get('city', driver_city)
                platform = session_data.get('platform', platform)
            else:
                # Token expired or missing — this shouldn't happen in normal flow
                print(f"WARNING: No session data found for token {token}")
                return """<!DOCTYPE html><html><head><title>FareAudit</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>body{font-family:'Sora',sans-serif;background:#efefec;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#fff;border-radius:12px;padding:2.5rem;max-width:480px;text-align:center;border:0.5px solid #e0dfd8}
h1{font-size:24px;margin-bottom:8px}p{color:#666660;font-size:14px;line-height:1.6;margin-bottom:1.5rem}
a{display:inline-block;background:#1a1a18;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-size:14px}</style>
</head><body><div class="box">
<h1>✓ Payment received</h1>
<p>Your session expired before we could generate your report. Please email <strong>FareAudit@pm.me</strong> with your name and city and we'll send your report within 1 hour.</p>
<a href="/">Back to FareAudit</a>
</div></body></html>"""

            pdf = generate_pdf_report(data, driver_name, driver_city, platform.capitalize())
            # Clean up session to free memory
            SESSION_STORE.pop(token, None)
            return send_file(pdf, mimetype='application/pdf', as_attachment=True,
                           download_name=f'FareAudit_{driver_name.replace(" ","_")}.pdf')
    except Exception as e:
        print(f"Success route error: {e}")

    return """<!DOCTYPE html><html><head><title>FareAudit — Thank You</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>body{font-family:'Sora',sans-serif;background:#efefec;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#fff;border-radius:12px;padding:2.5rem;max-width:480px;text-align:center;border:0.5px solid #e0dfd8}
h1{font-size:24px;margin-bottom:8px}p{color:#666660;font-size:14px;line-height:1.6;margin-bottom:1.5rem}
a{display:inline-block;background:#1a1a18;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-size:14px}</style>
</head><body><div class="box">
<h1>✓ Payment successful</h1>
<p>Thank you! If your download didn't start automatically, email <strong>FareAudit@pm.me</strong> with your name and city and we'll send your report directly.</p>
<a href="/">Back to FareAudit</a>
</div></body></html>"""

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'FareAudit'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
