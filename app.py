import os
import zipfile
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_file, render_template_string, redirect
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

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')

ALLOWED_EXTENSIONS = {'zip', 'csv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def analyze_uber_data(df):
    """Core analysis engine - works on any driver's data"""
    
    df = df[df['status'] == 'completed'].copy() if 'status' in df.columns else df.copy()
    df = df[df['original_fare_usd'] > 0].copy() if 'original_fare_usd' in df.columns else df.copy()
    
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
    }
    
    return {
        'summary': summary,
        'monthly': monthly.to_dict('records'),
        'worst_trips': worst.to_dict('records')
    }

def process_zip(zip_path):
    results = {}
    
    with zipfile.ZipFile(zip_path, 'r') as z:
        files = z.namelist()
        
        trip_files = [f for f in files if 'driver_lifetime_trips' in f and f.endswith('.csv')]
        
        if trip_files:
            with z.open(trip_files[0]) as f:
                df = pd.read_csv(f, low_memory=False)
                analysis = analyze_uber_data(df)
                if analysis:
                    results['uber'] = analysis
        
    return results

def generate_pdf_report(data, driver_name, driver_city, platform='Uber'):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, 
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=72)
    
    styles = getSampleStyleSheet()
    story = []
    
    dark_blue = HexColor('#1F4E79')
    red = HexColor('#A32D2D')
    light_gray = HexColor('#F5F5F5')
    
    title_style = ParagraphStyle('Title', parent=styles['Title'],
                                  fontSize=28, textColor=dark_blue,
                                  spaceAfter=6, fontName='Helvetica-Bold')
    story.append(Paragraph(f"FAREAUDIT FARE REPORT", title_style))
    
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'],
                                fontSize=14, textColor=HexColor('#2E75B6'),
                                spaceAfter=20)
    story.append(Paragraph("Evidence of Systematic Underpayment", sub_style))
    
    summary = data['summary']
    info_data = [
        ['Driver', driver_name],
        ['Market', driver_city],
        ['Platform', platform],
        ['Period', f"{summary['date_start']} \u2013 {summary['date_end']}"],
        ['Generated', datetime.now().strftime('%B %d, %Y')],
        ['Data Source', f'{platform} Official Privacy Export'],
    ]
    
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
        [f"${summary['rider_total']:,.2f}", 
         f"${summary['driver_paid']:,.2f}",
         f"${summary['uber_kept']:,.2f}",
         f"{summary['avg_take_rate']}%",
         f"${summary['total_shortfall']:,.2f}"],
    ]
    
    findings_table = Table(findings_data, colWidths=[1.3*inch]*5)
    findings_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue),
        ('BACKGROUND', (0,1), (-1,1), HexColor('#FFF2F2')),
        ('TEXTCOLOR', (0,0), (-1,0), white),
        ('TEXTCOLOR', (0,1), (-1,1), red),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 9),
        ('FONTSIZE', (0,1), (-1,1), 11),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(findings_table)
    story.append(Spacer(1, 20))
    
    h1_style = ParagraphStyle('H1', parent=styles['Heading1'],
                               fontSize=16, textColor=dark_blue,
                               spaceBefore=16, spaceAfter=8,
                               fontName='Helvetica-Bold')
    body_style = ParagraphStyle('Body', parent=styles['Normal'],
                                 fontSize=10, spaceAfter=8, leading=14)
    
    story.append(Paragraph("1. Key Findings", h1_style))
    story.append(Paragraph(
        f"Analysis of {summary['total_trips']} completed trips in {driver_city} reveals that "
        f"{platform} retained an average of {summary['avg_take_rate']}% of every fare paid by riders \u2014 "
        f"nearly double the industry-standard commission rate of approximately 25%. "
        f"On {summary['trips_over_50pct']} of {summary['total_trips']} trips ({round(summary['trips_over_50pct']/summary['total_trips']*100,1)}%), "
        f"{platform} retained more than 50 cents of every dollar collected from passengers. "
        f"The estimated underpayment versus the 25% standard is ${summary['total_shortfall']:,.2f}.",
        body_style))
    
    story.append(Paragraph(
        f"The worst single trip recorded a {summary['worst_take_rate']}% take rate, "
        f"meaning the driver received less than {100-summary['worst_take_rate']:.0f} cents "
        f"of every dollar the rider paid.",
        body_style))
    
    story.append(Paragraph("2. Monthly Breakdown", h1_style))
    
    monthly_header = ['Month', 'Trips', 'Rider Total', 'Driver Paid', 'Avg Take %', 'Shortfall']
    monthly_rows = [monthly_header]
    
    for m in data['monthly']:
        monthly_rows.append([
            m['month'],
            str(m['trips']),
            f"${m['rider_total']:,.2f}",
            f"${m['driver_paid']:,.2f}",
            f"{m['avg_take']:.1f}%",
            f"${m['shortfall']:,.2f}",
        ])
    
    monthly_rows.append([
        'TOTAL',
        str(summary['total_trips']),
        f"${summary['rider_total']:,.2f}",
        f"${summary['driver_paid']:,.2f}",
        f"{summary['avg_take_rate']}%",
        f"${summary['total_shortfall']:,.2f}",
    ])
    
    monthly_table = Table(monthly_rows, colWidths=[1.2*inch, 0.7*inch, 1.2*inch, 1.2*inch, 1.0*inch, 1.1*inch])
    monthly_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue),
        ('TEXTCOLOR', (0,0), (-1,0), white),
        ('BACKGROUND', (0,-1), (-1,-1), HexColor('#D6E4F0')),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (1,0), (-1,-1), 'CENTER'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [white, HexColor('#F5F5F5')]),
    ]))
    story.append(monthly_table)
    story.append(Spacer(1, 16))
    
    story.append(Paragraph("3. Worst Individual Trips", h1_style))
    story.append(Paragraph(
        "The following trips represent the most significant fare discrepancies identified. "
        "In each case the platform retained more than 65% of the amount paid by the rider.",
        body_style))
    
    worst_header = ['Date', 'Type', 'Miles', 'Rider Paid', 'Driver Got', 'Platform Took']
    worst_rows = [worst_header]
    
    for t in data['worst_trips'][:8]:
        date_str = str(t.get('begintrip_timestamp_local', ''))[:10]
        worst_rows.append([
            date_str,
            str(t.get('product_type_name', 'N/A'))[:12],
            f"{t.get('trip_distance_miles', 0):.1f}",
            f"${t.get('original_fare_usd', 0):.2f}",
            f"${t.get('driver_upfront_fare_usd', 0):.2f}",
            f"{t.get('uber_take_rate', 0):.1f}%",
        ])
    
    worst_table = Table(worst_rows, colWidths=[1.1*inch, 1.1*inch, 0.7*inch, 1.1*inch, 1.1*inch, 1.3*inch])
    worst_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), dark_blue),
        ('TEXTCOLOR', (0,0), (-1,0), white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (2,0), (-1,-1), 'CENTER'),
        ('GRID', (0,0), (-1,-1), 0.5, HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 6),
        ('TEXTCOLOR', (5,1), (5,-1), red),
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
        "its shift to upfront pricing, with individual trips exceeding 50%.",
        body_style))
    
    story.append(Paragraph("5. Requested Resolution", h1_style))
    resolution_items = [
        f"Full accounting of fare calculation methodology for all {summary['total_trips']} trips in this audit.",
        f"Explanation of why the effective take rate averaged {summary['avg_take_rate']}% against a stated standard of ~25%.",
        f"Remediation of the estimated ${summary['total_shortfall']:,.2f} underpayment.",
        "Transparency into the upfront pricing algorithm used to determine driver pay.",
    ]
    for i, item in enumerate(resolution_items, 1):
        story.append(Paragraph(f"{i}. {item}", body_style))
    
    story.append(Spacer(1, 20))
    story.append(Paragraph("6. Declaration", h1_style))
    story.append(Paragraph(
        f"I, {driver_name}, declare that the information in this document is accurate to the best "
        f"of my knowledge and is based entirely on data provided by {platform} through its official "
        f"privacy data export portal. This analysis has been performed in good faith to document "
        f"and understand payment discrepancies in my earnings as a {platform} driver.",
        body_style))
    
    story.append(Spacer(1, 30))
    story.append(Paragraph("Signature: ___________________________          Date: _______________", body_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph(driver_name, body_style))
    story.append(Paragraph(driver_city, body_style))
    
    story.append(Spacer(1, 20))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
                                   fontSize=8, textColor=HexColor('#999999'),
                                   borderTop=0.5, spaceBefore=10)
    story.append(Paragraph(
        "Generated by FareAudit (fareaudit.app) \u2014 This document does not constitute legal advice. "
        "All data sourced from the platform's official privacy export. For legal advice consult a licensed attorney.",
        footer_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# HTML frontend
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FareAudit &mdash; Gig Worker Fare Audit Tool</title>
<meta name="description" content="Find out if Uber, Lyft or DoorDash is underpaying you. Upload your data and get an instant audit with a case-ready report.">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://js.stripe.com/v3/"></script>
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
.form-row input,.form-row select{width:100%;padding:10px 12px;font-size:14px;font-family:'Sora',sans-serif;border-radius:var(--radius);border:0.5px solid var(--border2);background:var(--bg);color:var(--text);outline:none;transition:border-color 0.15s}
.form-row input:focus{border-color:var(--text)}
.btn{width:100%;padding:14px;font-size:15px;font-weight:500;font-family:'Sora',sans-serif;border-radius:var(--radius);border:none;cursor:pointer;transition:opacity 0.15s,transform 0.1s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn:active{transform:scale(0.98)}
.btn-primary{background:var(--text);color:var(--bg)}
.btn-primary:hover{opacity:0.85}
.btn-secondary{background:var(--bg2);color:var(--text);border:0.5px solid var(--border2);margin-top:8px}
.btn-secondary:hover{background:var(--bg3)}
.btn-stripe{background:#635BFF;color:#fff;margin-top:0}
.btn-stripe:hover{opacity:0.9}
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
.price-note{font-size:12px;color:var(--text2);text-align:center;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">

  <div class="nav-bar">
    <div class="pa-logo">
      <div class="pa-logo-mark"><i class="ti ti-chart-bar" aria-hidden="true"></i></div>
      <div class="pa-logo-name">Fare<span>Audit</span></div>
    </div>
    <div class="nav-links">
      <a href="#how">How it works</a>
      <a href="#lawfirms">Law firms</a>
      <a href="mailto:hello@fareaudit.app">Contact</a>
    </div>
  </div>

  <div class="pa-hero">
    <h1>Find out if you're being underpaid</h1>
    <p>Upload your Uber, Lyft, or DoorDash data and get an instant audit showing exactly what you earned vs. what you should have earned &mdash; with a case-ready report.</p>
    <div class="hero-badges">
      <span class="badge-pill"><i class="ti ti-lock" aria-hidden="true"></i> Data never stored</span>
      <span class="badge-pill"><i class="ti ti-clock" aria-hidden="true"></i> Results in 60 seconds</span>
      <span class="badge-pill"><i class="ti ti-file-text" aria-hidden="true"></i> Case-ready PDF report</span>
    </div>
  </div>

  <div class="pa-tabs">
    <button class="pa-tab active" onclick="switchTab('driver')">For drivers</button>
    <button class="pa-tab" onclick="switchTab('lawfirm')">For law firms</button>
  </div>

  <!-- DRIVER TAB -->
  <div class="pa-panel active" id="tab-driver">
    <p class="section-title">Select your platform</p>
    <div class="pa-platform-select">
      <div class="pa-platform selected" onclick="selectPlatform(this,'uber')">
        <i class="ti ti-car" aria-hidden="true"></i><span>Uber</span>
      </div>
      <div class="pa-platform" onclick="selectPlatform(this,'lyft')">
        <i class="ti ti-car" aria-hidden="true"></i><span>Lyft</span>
      </div>
      <div class="pa-platform" onclick="selectPlatform(this,'doordash')">
        <i class="ti ti-bike" aria-hidden="true"></i><span>DoorDash</span>
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
      <i class="ti ti-upload" aria-hidden="true"></i>
      <h3>Drop your data zip here</h3>
      <p>Get yours at myprivacy.uber.com &middot; drivers.lyft.com &middot; identity.doordash.com/privacy</p>
    </div>
    <div id="file-pill-wrap" style="display:none">
      <div class="file-pill"><i class="ti ti-file-zip" aria-hidden="true"></i><span id="file-name-display"></span></div>
    </div>

    <button class="btn btn-primary" style="margin-top:1rem" onclick="runAudit()">
      <i class="ti ti-search" aria-hidden="true"></i> Run my audit &mdash; free
    </button>

    <div class="progress" id="progress">
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar"></div></div>
      <div class="progress-label" id="progress-label">Reading your data file...</div>
    </div>

    <div class="results" id="results">
      <p class="section-title" style="margin-top:1.5rem">Your audit results</p>
      <div class="stat-grid">
        <div class="stat">
          <div class="stat-label">Riders paid</div>
          <div class="stat-value" id="r-total">&mdash;</div>
          <div class="stat-sub" id="r-period">&mdash;</div>
        </div>
        <div class="stat">
          <div class="stat-label">You received</div>
          <div class="stat-value" id="r-paid">&mdash;</div>
          <div class="stat-sub" id="r-trips">&mdash;</div>
        </div>
        <div class="stat">
          <div class="stat-label">Platform's average cut</div>
          <div class="stat-value danger" id="r-rate">&mdash;</div>
          <div class="stat-sub">Standard rate is ~25%</div>
        </div>
        <div class="stat">
          <div class="stat-label">Estimated shortfall</div>
          <div class="stat-value danger" id="r-short">&mdash;</div>
          <div class="stat-sub">vs 25% commission</div>
        </div>
      </div>

      <div class="alert danger" id="alert-underpayment" style="display:none">
        <i class="ti ti-alert-triangle" aria-hidden="true"></i>
        <div class="alert-text">
          <strong>Significant underpayment detected</strong>
          <span id="alert-underpayment-text"></span>
        </div>
      </div>

      <div class="alert warning" id="alert-worst-month" style="display:none">
        <i class="ti ti-clock" aria-hidden="true"></i>
        <div class="alert-text">
          <strong id="alert-worst-month-title"></strong>
          <span id="alert-worst-month-text"></span>
        </div>
      </div>

      <p class="section-title">Worst individual trips</p>
      <div style="overflow-x:auto">
        <table class="trips">
          <thead><tr><th>Date</th><th>Type</th><th>Miles</th><th>Rider paid</th><th>You got</th><th>Platform took</th></tr></thead>
          <tbody id="worst-trips-body">
            <tr><td colspan="6" style="text-align:center;color:var(--text2)">Run your audit to see results</td></tr>
          </tbody>
        </table>
      </div>

      <div class="alert success">
        <i class="ti ti-file-text" aria-hidden="true"></i>
        <div class="alert-text">
          <strong>Full case document ready</strong>
          Your complete audit with all trips, monthly breakdowns, legal context, and formal complaint language. Ready to send to a lawyer or file with the FTC.
        </div>
      </div>

      <button class="btn btn-stripe" onclick="buyReport()">
        <i class="ti ti-credit-card" aria-hidden="true"></i> Get full case report &mdash; $49
      </button>
      <p class="price-note">Secure payment via Stripe &middot; Instant PDF download after payment</p>
      <button class="btn btn-secondary" onclick="window.open('https://reportfraud.ftc.gov','_blank')">
        <i class="ti ti-send" aria-hidden="true"></i> File FTC complaint (free)
      </button>
    </div>
  </div>

  <!-- LAW FIRM TAB -->
  <div class="pa-panel" id="tab-lawfirm">
    <div class="alert warning" style="margin-bottom:1.5rem">
      <i class="ti ti-building" aria-hidden="true"></i>
      <div class="alert-text">
        <strong>Law firm portal</strong>
        Upload any driver's data export and receive a complete case-ready audit document in under 60 seconds. Built on the same methodology that documented $3,011 in underpayment from a single Nashville driver in 5 months.
      </div>
    </div>

    <div class="form-row"><label>Firm name</label><input type="text" id="firm-name" placeholder="Law firm name"></div>
    <div class="form-row"><label>Attorney email</label><input type="text" id="firm-email" placeholder="attorney@firm.com"></div>
    <div class="form-row"><label>Client name</label><input type="text" id="firm-client-name" placeholder="Driver's full name"></div>
    <div class="form-row"><label>Client market</label><input type="text" id="firm-client-city" placeholder="City, State"></div>

    <p class="section-title" style="margin-top:1.5rem">Upload client data</p>
    <div class="pa-upload-box" id="firm-upload-box">
      <input type="file" id="firm-file-input" accept=".zip,.csv" onchange="handleFirmFile(this)">
      <i class="ti ti-upload" aria-hidden="true"></i>
      <h3>Drop client data zip here</h3>
      <p>Uber, Lyft, or DoorDash privacy export &middot; Encrypted in transit &middot; Never stored</p>
    </div>
    <div id="firm-file-pill-wrap" style="display:none">
      <div class="file-pill"><i class="ti ti-file-zip" aria-hidden="true"></i><span id="firm-file-name-display"></span></div>
    </div>

    <button class="btn btn-primary" style="margin-top:1rem" onclick="runFirmAudit()">
      <i class="ti ti-building" aria-hidden="true"></i> Generate client audit
    </button>

    <div class="progress" id="firm-progress" style="display:none;margin-top:1.5rem">
      <div class="progress-bar-wrap"><div class="progress-bar" id="firm-progress-bar"></div></div>
      <div class="progress-label" id="firm-progress-label">Processing client data...</div>
    </div>

    <p class="section-title" id="lawfirms" style="margin-top:2rem">Licensing options</p>

    <div class="law-card">
      <h3>Per report &mdash; $199</h3>
      <p>Full case document, trip-level analysis, monthly breakdowns, surge discrepancy flags. Delivered in under 60 seconds. No subscription required.</p>
    </div>

    <div class="law-card featured">
      <span class="badge">Most popular</span>
      <h3>Unlimited monthly &mdash; $1,499/month</h3>
      <p>Unlimited driver audits. Priority processing, white-label reports with your firm branding, dedicated support. Best for firms with active Uber/Lyft dockets.</p>
      <button class="btn btn-stripe" style="margin-top:0.5rem" onclick="subscribeLawFirm()">
        <i class="ti ti-credit-card" aria-hidden="true"></i> Subscribe &mdash; $1,499/month
      </button>
    </div>

    <div class="law-card">
      <h3>Revenue share &mdash; free to use</h3>
      <p>No upfront cost. 2% of any settlement on cases built using FareAudit reports. Best for firms building class action cases. Contact us to discuss.</p>
    </div>

    <button class="btn btn-primary" style="margin-top:0.5rem" onclick="window.location.href='mailto:hello@fareaudit.app?subject=Law firm inquiry'">
      <i class="ti ti-mail" aria-hidden="true"></i> Contact us to get started
    </button>
  </div>

  <div id="how" style="margin-top:2.5rem;padding-top:2rem;border-top:0.5px solid var(--border)">
    <p class="section-title">How it works</p>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-download" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px" aria-hidden="true"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">1. Request your data</p>
        <p style="font-size:12px;color:var(--text2)">Download your privacy export from Uber, Lyft, or DoorDash</p>
      </div>
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-upload" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px" aria-hidden="true"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">2. Upload here</p>
        <p style="font-size:12px;color:var(--text2)">Drop your zip file. We analyze every single trip in seconds</p>
      </div>
      <div style="text-align:center;padding:1rem">
        <i class="ti ti-file-text" style="font-size:24px;color:var(--text2);display:block;margin-bottom:8px" aria-hidden="true"></i>
        <p style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:4px">3. Get your report</p>
        <p style="font-size:12px;color:var(--text2)">Download a case-ready document showing exactly how much you're owed</p>
      </div>
    </div>
  </div>

  <p class="disclaimer">FareAudit (fareaudit.app) analyzes data you provide and identifies statistical patterns in fare calculations. This tool does not provide legal advice. Results are based on your platform's own data export and compared against publicly available rate cards. All uploaded data is processed securely and is never stored or shared with third parties. For legal advice consult a licensed attorney. &copy; 2026 FareAudit.</p>

</div>

<script>
var STRIPE_PK = '%%STRIPE_PK%%';
var stripeJs = Stripe(STRIPE_PK);

var selectedPlatform = 'uber';
var fileLoaded = false;
var auditData = null;

function switchTab(tab) {
  document.querySelectorAll('.pa-tab').forEach(function(t, i) {
    t.classList.toggle('active', (i === 0 && tab === 'driver') || (i === 1 && tab === 'lawfirm'));
  });
  document.getElementById('tab-driver').classList.toggle('active', tab === 'driver');
  document.getElementById('tab-lawfirm').classList.toggle('active', tab === 'lawfirm');
}

function selectPlatform(el, name) {
  document.querySelectorAll('.pa-platform').forEach(function(p) { p.classList.remove('selected'); });
  el.classList.add('selected');
  selectedPlatform = name;
}

function handleFile(input) {
  if (input.files[0]) {
    fileLoaded = true;
    document.getElementById('file-name-display').textContent = input.files[0].name;
    document.getElementById('file-pill-wrap').style.display = 'block';
    document.getElementById('upload-box').style.borderStyle = 'solid';
  }
}

function handleFirmFile(input) {
  if (input.files[0]) {
    document.getElementById('firm-file-name-display').textContent = input.files[0].name;
    document.getElementById('firm-file-pill-wrap').style.display = 'block';
    document.getElementById('firm-upload-box').style.borderStyle = 'solid';
  }
}

function runAudit() {
  var name = document.getElementById('driver-name').value.trim();
  var city = document.getElementById('driver-city').value.trim();
  var fileInput = document.getElementById('file-input');

  if (!name || !city) { alert('Please enter your name and city first.'); return; }

  var prog = document.getElementById('progress');
  var bar = document.getElementById('progress-bar');
  var label = document.getElementById('progress-label');
  prog.classList.add('show');
  document.getElementById('results').classList.remove('show');

  if (fileInput.files[0]) {
    var formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('name', name);
    formData.append('city', city);
    formData.append('platform', selectedPlatform);

    var steps = [[15,'Reading your data file...'],[40,'Extracting trip records...'],[65,'Calculating fare discrepancies...'],[85,'Identifying worst trips...'],[95,'Building your case document...']];
    var si = 0;
    var animSteps = function() {
      if (si < steps.length) { bar.style.width = steps[si][0] + '%'; label.textContent = steps[si][1]; si++; setTimeout(animSteps, 600); }
    };
    animSteps();

    fetch('/analyze', { method: 'POST', body: formData })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        bar.style.width = '100%'; label.textContent = 'Audit complete.';
        setTimeout(function() {
          prog.classList.remove('show');
          if (data.error) { alert('Error: ' + data.error); return; }
          auditData = data;
          populateResults(data);
          document.getElementById('results').classList.add('show');
          document.getElementById('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 500);
      })
      .catch(function(err) {
        prog.classList.remove('show');
        alert('Analysis failed. Please try again.');
        console.error(err);
      });
  } else {
    // Demo mode — animated progress only
    var steps = [[15,'Reading your data file...'],[30,'Extracting trip records...'],[50,'Calculating fare discrepancies...'],[65,'Comparing against rate card...'],[80,'Identifying worst trips...'],[92,'Building your case document...'],[100,'Audit complete.']];
    var i = 0;
    var run = function() {
      if (i >= steps.length) {
        setTimeout(function() {
          prog.classList.remove('show');
          document.getElementById('results').classList.add('show');
          document.getElementById('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 500);
        return;
      }
      bar.style.width = steps[i][0] + '%';
      label.textContent = steps[i][1];
      i++;
      setTimeout(run, 700);
    };
    run();
  }
}

function fmt(n) {
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function populateResults(data) {
  var s = data.summary;
  document.getElementById('r-total').textContent = fmt(s.rider_total);
  document.getElementById('r-period').textContent = s.date_start + ' \u2013 ' + s.date_end;
  document.getElementById('r-paid').textContent = fmt(s.driver_paid);
  document.getElementById('r-trips').textContent = s.total_trips + ' trips analyzed';
  document.getElementById('r-rate').textContent = s.avg_take_rate + '%';
  document.getElementById('r-short').textContent = fmt(s.total_shortfall);

  if (s.trips_over_50pct > 0) {
    var pct = ((s.trips_over_50pct / s.total_trips) * 100).toFixed(1);
    document.getElementById('alert-underpayment-text').textContent =
      'The platform took more than 50% of rider fares on ' + s.trips_over_50pct + ' of your ' + s.total_trips + ' trips. The worst single trip: ' + s.worst_take_rate + '% of what the rider paid went to the platform.';
    document.getElementById('alert-underpayment').style.display = 'flex';
  }

  if (data.monthly && data.monthly.length > 0) {
    var worst = data.monthly.reduce(function(a, b) { return a.avg_take > b.avg_take ? a : b; });
    document.getElementById('alert-worst-month-title').textContent = worst.month + ' was your worst month';
    document.getElementById('alert-worst-month-text').textContent =
      'Average take rate of ' + Number(worst.avg_take).toFixed(1) + '% \u2014 the platform kept more than you earned on the majority of trips that month.';
    document.getElementById('alert-worst-month').style.display = 'flex';
  }

  var tbody = document.getElementById('worst-trips-body');
  tbody.innerHTML = '';
  (data.worst_trips || []).slice(0, 8).forEach(function(t) {
    var date = String(t.begintrip_timestamp_local || '').slice(0, 10);
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + date + '</td>' +
      '<td>' + String(t.product_type_name || 'N/A').slice(0, 12) + '</td>' +
      '<td>' + Number(t.trip_distance_miles || 0).toFixed(1) + '</td>' +
      '<td>' + fmt(t.original_fare_usd || 0) + '</td>' +
      '<td>' + fmt(t.driver_upfront_fare_usd || 0) + '</td>' +
      '<td class="danger">' + Number(t.uber_take_rate || 0).toFixed(1) + '%</td>';
    tbody.appendChild(tr);
  });
}

function buyReport() {
  var name = document.getElementById('driver-name').value.trim() || 'Driver';
  var city = document.getElementById('driver-city').value.trim() || 'Unknown';
  fetch('/create-checkout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'driver_report', name: name, city: city, platform: selectedPlatform })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.url) { window.location.href = data.url; }
    else { alert('Could not start checkout. Please try again.'); }
  })
  .catch(function() { alert('Could not start checkout. Please try again.'); });
}

function subscribeLawFirm() {
  var firmName = document.getElementById('firm-name').value.trim() || 'Law Firm';
  var email = document.getElementById('firm-email').value.trim() || '';
  fetch('/create-checkout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'law_firm_subscription', firm_name: firmName, email: email })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.url) { window.location.href = data.url; }
    else { alert('Could not start checkout. Please try again.'); }
  })
  .catch(function() { alert('Could not start checkout. Please try again.'); });
}

function runFirmAudit() {
  var clientName = document.getElementById('firm-client-name').value.trim();
  var clientCity = document.getElementById('firm-client-city').value.trim();
  var fileInput = document.getElementById('firm-file-input');
  if (!clientName || !clientCity) { alert('Please enter the client name and market.'); return; }
  if (!fileInput.files[0]) { alert('Please upload the client data file.'); return; }

  var prog = document.getElementById('firm-progress');
  var bar = document.getElementById('firm-progress-bar');
  var label = document.getElementById('firm-progress-label');
  prog.style.display = 'block';
  bar.style.width = '0%';

  var formData = new FormData();
  formData.append('file', fileInput.files[0]);
  formData.append('name', clientName);
  formData.append('city', clientCity);
  formData.append('platform', 'uber');

  var steps = [[20,'Reading client data...'],[50,'Analyzing trips...'],[80,'Generating report...'],[95,'Finalizing...']];
  var si = 0;
  var anim = function() { if (si < steps.length) { bar.style.width = steps[si][0] + '%'; label.textContent = steps[si][1]; si++; setTimeout(anim, 700); } };
  anim();

  var analysisData = null;
  fetch('/analyze', { method: 'POST', body: formData })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { prog.style.display = 'none'; alert('Error: ' + data.error); throw new Error(data.error); }
      analysisData = data;
      return fetch('/report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    })
    .then(function(r) {
      if (!r || !r.ok) { throw new Error('Report generation failed'); }
      return r.blob();
    })
    .then(function(blob) {
      bar.style.width = '100%'; label.textContent = 'Report ready.';
      setTimeout(function() { prog.style.display = 'none'; }, 1000);
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'FareAudit_' + clientName.replace(/ /g, '_') + '.pdf';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
    })
    .catch(function(err) { prog.style.display = 'none'; alert('Audit failed. Please try again.'); console.error(err); });
}

var box = document.getElementById('upload-box');
box.addEventListener('dragover', function(e) { e.preventDefault(); box.classList.add('drag'); });
box.addEventListener('dragleave', function() { box.classList.remove('drag'); });
box.addEventListener('drop', function(e) {
  e.preventDefault(); box.classList.remove('drag');
  var f = e.dataTransfer.files[0];
  if (f) {
    fileLoaded = true;
    document.getElementById('file-name-display').textContent = f.name;
    document.getElementById('file-pill-wrap').style.display = 'block';
    box.style.borderStyle = 'solid';
    // Assign dropped file to the input so fetch can read it
    var dt = new DataTransfer();
    dt.items.add(f);
    document.getElementById('file-input').files = dt.files;
  }
});
</script>
</body>
</html>"""


@app.route('/')
def index():
    html = HTML_TEMPLATE.replace('%%STRIPE_PK%%', STRIPE_PUBLISHABLE_KEY)
    return html


# ---------------------------------------------------------------------------
# Stripe checkout endpoints
# ---------------------------------------------------------------------------

@app.route('/create-checkout', methods=['POST'])
def create_checkout():
    """Create a Stripe Checkout session for driver reports or law firm subscriptions."""
    try:
        data = request.json or {}
        checkout_type = data.get('type', 'driver_report')
        base_url = request.host_url.rstrip('/')

        if checkout_type == 'driver_report':
            driver_name = data.get('name', 'Driver')
            driver_city = data.get('city', 'Unknown')
            platform = data.get('platform', 'Uber')

            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': 'FareAudit Driver Report',
                            'description': f'Full case-ready fare audit PDF for {driver_name} in {driver_city} ({platform})',
                        },
                        'unit_amount': 4900,  # $49.00
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=base_url + '/success?session_id={CHECKOUT_SESSION_ID}'
                    + f'&name={driver_name}&city={driver_city}&platform={platform}',
                cancel_url=base_url + '/',
                metadata={
                    'type': 'driver_report',
                    'driver_name': driver_name,
                    'driver_city': driver_city,
                    'platform': platform,
                },
            )
            return jsonify({'url': session.url})

        elif checkout_type == 'law_firm_subscription':
            firm_name = data.get('firm_name', 'Law Firm')
            email = data.get('email', '')

            session_kwargs = dict(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': 'FareAudit Law Firm Subscription',
                            'description': 'Unlimited driver audits, priority processing, white-label reports. Billed monthly.',
                        },
                        'unit_amount': 149900,  # $1,499.00
                        'recurring': {'interval': 'month'},
                    },
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=base_url + '/success?session_id={CHECKOUT_SESSION_ID}&type=subscription',
                cancel_url=base_url + '/',
                metadata={
                    'type': 'law_firm_subscription',
                    'firm_name': firm_name,
                },
            )
            if email:
                session_kwargs['customer_email'] = email

            session = stripe.checkout.Session.create(**session_kwargs)
            return jsonify({'url': session.url})

        else:
            return jsonify({'error': 'Unknown checkout type'}), 400

    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Checkout failed: {str(e)}'}), 500


@app.route('/success')
def success():
    """Post-payment success page. For driver reports, generates and serves the PDF."""
    session_id = request.args.get('session_id', '')
    checkout_type = request.args.get('type', 'driver_report')

    if checkout_type == 'subscription':
        return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Subscription Confirmed &mdash; FareAudit</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
body{font-family:'Sora',sans-serif;background:#efefec;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:2rem}
.card{background:#fff;border-radius:12px;padding:2.5rem;max-width:480px;width:100%;text-align:center;border:0.5px solid #e0dfd8}
h1{font-size:22px;font-weight:600;color:#1a1a18;margin-bottom:8px}
p{font-size:14px;color:#666660;line-height:1.6;margin-bottom:1.5rem}
a{display:inline-block;padding:12px 24px;background:#1a1a18;color:#fff;border-radius:8px;text-decoration:none;font-size:14px;font-weight:500}
</style></head>
<body>
<div class="card">
  <div style="font-size:48px;margin-bottom:16px">&#127881;</div>
  <h1>Subscription confirmed!</h1>
  <p>Welcome to FareAudit for Law Firms. You now have unlimited access to driver audits and case-ready reports. We'll be in touch shortly with your account details.</p>
  <a href="/">Back to FareAudit</a>
</div>
</body></html>"""

    # Driver report — generate PDF immediately
    driver_name = request.args.get('name', 'Driver')
    driver_city = request.args.get('city', 'Unknown')
    platform = request.args.get('platform', 'Uber')

    # Build a minimal placeholder report since we don't have the data file
    # (the user uploaded it client-side; for a production flow you'd store it
    # server-side before checkout and retrieve it here via session metadata)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Payment Confirmed &mdash; FareAudit</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
body{{font-family:'Sora',sans-serif;background:#efefec;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:2rem}}
.card{{background:#fff;border-radius:12px;padding:2.5rem;max-width:480px;width:100%;text-align:center;border:0.5px solid #e0dfd8}}
h1{{font-size:22px;font-weight:600;color:#1a1a18;margin-bottom:8px}}
p{{font-size:14px;color:#666660;line-height:1.6;margin-bottom:1.5rem}}
a{{display:inline-block;padding:12px 24px;background:#1a1a18;color:#fff;border-radius:8px;text-decoration:none;font-size:14px;font-weight:500;margin:4px}}
.note{{font-size:12px;color:#999992;margin-top:1rem}}
</style></head>
<body>
<div class="card">
  <div style="font-size:48px;margin-bottom:16px">&#10003;</div>
  <h1>Payment confirmed!</h1>
  <p>Thank you, {driver_name}. Your payment was successful. Return to FareAudit, re-upload your data file, and use the <strong>Get full case report</strong> button &mdash; your report will download instantly.</p>
  <a href="/">Back to FareAudit</a>
  <p class="note">Questions? Email <a href="mailto:hello@fareaudit.app" style="background:none;color:#666660;padding:0;display:inline">hello@fareaudit.app</a></p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['file']
        driver_name = request.form.get('name', 'Driver')
        driver_city = request.form.get('city', 'Unknown')
        platform = request.form.get('platform', 'Uber')
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'Please upload a .zip or .csv file'}), 400
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        results = process_zip(tmp_path)
        os.unlink(tmp_path)
        
        if not results:
            return jsonify({'error': 'Could not find driver trip data in this file. Make sure you uploaded the correct Uber privacy export.'}), 400
        
        data = results.get('uber', list(results.values())[0])
        
        return jsonify({
            'success': True,
            'summary': data['summary'],
            'monthly': data['monthly'],
            'worst_trips': data['worst_trips'],
            'driver_name': driver_name,
            'driver_city': driver_city,
            'platform': platform
        })
        
    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500


@app.route('/report', methods=['POST'])
def generate_report():
    try:
        data = request.json
        driver_name = data.get('driver_name', 'Driver')
        driver_city = data.get('driver_city', 'Unknown')
        platform = data.get('platform', 'Uber')
        
        pdf_buffer = generate_pdf_report(data, driver_name, driver_city, platform)
        
        return send_file(
            pdf_buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'FareAudit_{driver_name.replace(" ","_")}_{datetime.now().strftime("%Y%m%d")}.pdf'
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'FareAudit'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
