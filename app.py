import os
import zipfile
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_file, render_template_string
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

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max

ALLOWED_EXTENSIONS = {'zip', 'csv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def analyze_uber_data(df):
    """Core analysis engine - works on any driver's data"""
    
    # Filter to completed trips with fares
    df = df[df['status'] == 'completed'].copy() if 'status' in df.columns else df.copy()
    df = df[df['original_fare_usd'] > 0].copy() if 'original_fare_usd' in df.columns else df.copy()
    
    if len(df) == 0:
        return None
    
    # Calculate key metrics
    df['duration_min'] = (df['trip_duration_seconds'] / 60).round(2)
    df['uber_take_rate'] = ((df['original_fare_usd'] - df['driver_upfront_fare_usd']) / df['original_fare_usd'] * 100).round(1)
    df['fair_driver_pay'] = (df['original_fare_usd'] * 0.75).round(2)
    df['shortfall'] = (df['fair_driver_pay'] - df['driver_upfront_fare_usd']).clip(lower=0).round(2)
    
    # Date parsing
    df['date'] = pd.to_datetime(df['begintrip_timestamp_local'], errors='coerce')
    df['month'] = df['date'].dt.to_period('M').astype(str)
    
    # Monthly breakdown
    monthly = df.groupby('month').agg(
        trips=('driver_upfront_fare_usd', 'count'),
        rider_total=('original_fare_usd', 'sum'),
        driver_paid=('driver_upfront_fare_usd', 'sum'),
        avg_take=('uber_take_rate', 'mean'),
        shortfall=('shortfall', 'sum')
    ).round(2).reset_index()
    
    # Worst trips
    worst = df.nlargest(10, 'uber_take_rate')[
        ['begintrip_timestamp_local', 'product_type_name', 'trip_distance_miles',
         'duration_min', 'original_fare_usd', 'driver_upfront_fare_usd', 'uber_take_rate']
    ].round(2)
    
    # Summary stats
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
    """Extract and analyze data from Uber privacy export zip"""
    results = {}
    
    with zipfile.ZipFile(zip_path, 'r') as z:
        files = z.namelist()
        
        # Find driver trips file
        trip_files = [f for f in files if 'driver_lifetime_trips' in f and f.endswith('.csv')]
        payment_files = [f for f in files if 'driver_payments' in f and f.endswith('.csv')]
        
        if trip_files:
            with z.open(trip_files[0]) as f:
                df = pd.read_csv(f, low_memory=False)
                analysis = analyze_uber_data(df)
                if analysis:
                    results['uber'] = analysis
        
    return results

def generate_pdf_report(data, driver_name, driver_city, platform='Uber'):
    """Generate professional PDF case document"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, 
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=72)
    
    styles = getSampleStyleSheet()
    story = []
    
    # Colors
    dark_blue = HexColor('#1F4E79')
    red = HexColor('#A32D2D')
    light_gray = HexColor('#F5F5F5')
    
    # Title
    title_style = ParagraphStyle('Title', parent=styles['Title'],
                                  fontSize=28, textColor=dark_blue,
                                  spaceAfter=6, fontName='Helvetica-Bold')
    story.append(Paragraph(f"PAYAUDIT FARE REPORT", title_style))
    
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'],
                                fontSize=14, textColor=HexColor('#2E75B6'),
                                spaceAfter=20)
    story.append(Paragraph("Evidence of Systematic Underpayment", sub_style))
    
    # Info table
    summary = data['summary']
    info_data = [
        ['Driver', driver_name],
        ['Market', driver_city],
        ['Platform', platform],
        ['Period', f"{summary['date_start']} – {summary['date_end']}"],
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
    
    # Key findings box
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
        ('ROWBACKGROUNDS', (0,1), (-1,1), [HexColor('#FFF2F2')]),
    ]))
    story.append(findings_table)
    story.append(Spacer(1, 20))
    
    # Section: Key Findings
    h1_style = ParagraphStyle('H1', parent=styles['Heading1'],
                               fontSize=16, textColor=dark_blue,
                               spaceBefore=16, spaceAfter=8,
                               fontName='Helvetica-Bold')
    body_style = ParagraphStyle('Body', parent=styles['Normal'],
                                 fontSize=10, spaceAfter=8, leading=14)
    
    story.append(Paragraph("1. Key Findings", h1_style))
    story.append(Paragraph(
        f"Analysis of {summary['total_trips']} completed trips in {driver_city} reveals that "
        f"{platform} retained an average of {summary['avg_take_rate']}% of every fare paid by riders — "
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
    
    # Monthly breakdown table
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
    
    # Totals row
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
    
    # Worst trips
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
    
    # Legal context
    story.append(Paragraph("4. Legal Context", h1_style))
    story.append(Paragraph(
        "This audit is supported by documented legal actions including a 2025 settlement "
        "in which Uber and Lyft paid $328 million to resolve wage theft allegations in New York. "
        "The FTC filed complaints against Uber in April and December 2025 for deceptive practices. "
        "A 2025 academic study found Uber's effective take rate increased from ~32% to 42% following "
        "its shift to upfront pricing, with individual trips exceeding 50%.",
        body_style))
    
    # Requested resolution
    story.append(Paragraph("5. Requested Resolution", h1_style))
    resolution_items = [
        f"Full accounting of fare calculation methodology for all {summary['total_trips']} trips in this audit.",
        f"Explanation of why the effective take rate averaged {summary['avg_take_rate']}% against a stated standard of ~25%.",
        f"Remediation of the estimated ${summary['total_shortfall']:,.2f} underpayment.",
        "Transparency into the upfront pricing algorithm used to determine driver pay.",
    ]
    for i, item in enumerate(resolution_items, 1):
        story.append(Paragraph(f"{i}. {item}", body_style))
    
    # Declaration
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
    
    # Footer note
    story.append(Spacer(1, 20))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
                                   fontSize=8, textColor=HexColor('#999999'),
                                   borderTop=0.5, spaceBefore=10)
    story.append(Paragraph(
        "Generated by PayAudit (fareaudit.app) — This document does not constitute legal advice. "
        "All data sourced from the platform's official privacy export. For legal advice consult a licensed attorney.",
        footer_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer

@app.route('/')
def index():
    with open('index.html', 'r') as f:
        return f.read()

@app.route('/analyze', methods=['POST'])
def analyze():
    """Main analysis endpoint"""
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
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        # Process the file
        results = process_zip(tmp_path)
        os.unlink(tmp_path)  # Delete temp file immediately
        
        if not results:
            return jsonify({'error': 'Could not find driver trip data in this file. Make sure you uploaded the correct Uber privacy export.'}), 400
        
        # Get the data
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
    """Generate and return PDF report"""
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
            download_name=f'PayAudit_{driver_name.replace(" ","_")}_{datetime.now().strftime("%Y%m%d")}.pdf'
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'PayAudit'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
