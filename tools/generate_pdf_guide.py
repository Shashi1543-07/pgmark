"""Generates a PDF setup guide for team members.

Tries reportlab if installed, or opens the printable SETUP_GUIDE.html
which allows 1-click 'Save as PDF' from any browser.
"""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "SETUP_GUIDE.html"
PDF_PATH = ROOT / "PUGMARK_Setup_Guide.pdf"


def generate_pdf_reportlab() -> bool:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        return False

    doc = SimpleDocTemplate(str(PDF_PATH), pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1e3a1e')
    )
    h2_style = ParagraphStyle(
        'DocH2',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=13,
        leading=16,
        textColor=colors.HexColor('#1e3a1e'),
        spaceBefore=12,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        'DocBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor('#24292e')
    )
    code_style = ParagraphStyle(
        'DocCode',
        parent=styles['Code'],
        fontName='Courier',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#1f2328'),
        backColor=colors.HexColor('#f4f6f3'),
        borderPadding=6
    )

    story = []
    story.append(Paragraph("PUGMARK — Team Setup & Onboarding Guide", title_style))
    story.append(Paragraph("Automated Camera-Trap Triage & Movement Intelligence Edge Node", body_style))
    story.append(Spacer(1, 10))

    # Prerequisites Table
    story.append(Paragraph("1. Prerequisites", h2_style))
    prereq_data = [
        [Paragraph("<b>Component</b>", body_style), Paragraph("<b>Requirement</b>", body_style), Paragraph("<b>Notes</b>", body_style)],
        [Paragraph("<b>Python</b>", body_style), Paragraph("3.10 or 3.11", body_style), Paragraph("Ensure 'Add to PATH' is checked", body_style)],
        [Paragraph("<b>Git</b>", body_style), Paragraph("Any recent version", body_style), Paragraph("For cloning and updating repository", body_style)],
        [Paragraph("<b>OS</b>", body_style), Paragraph("Windows / Linux / macOS", body_style), Paragraph("Runs completely offline on CPU laptops", body_style)]
    ]
    t = Table(prereq_data, colWidths=[90, 120, 320])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#e8eee6')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))

    # Step by Step
    story.append(Paragraph("2. Setup Steps", h2_style))

    story.append(Paragraph("<b>Step 1: Clone Repository</b>", body_style))
    story.append(Preformatted("git clone <repository-url>\ncd pugmark-v0.1.1", code_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Step 2: Virtual Environment</b>", body_style))
    story.append(Preformatted("# Windows\npython -m venv .venv\n.venv\\Scripts\\activate\n\n# Linux / macOS\npython3 -m venv .venv\nsource .venv/bin/activate", code_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Step 3: Install Dependencies</b>", body_style))
    story.append(Preformatted("python -m pip install --upgrade pip\npip install -r requirements.txt", code_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Step 4: Seed Demo Data & Admin User</b>", body_style))
    story.append(Preformatted("python -m tools.seed_demo\n# (Optional) Set custom admin password:\npython -m tools.emergency_reset_admin admin \"Admin@12345\"", code_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Step 5: Launch the Server</b>", body_style))
    story.append(Preformatted("# Launcher scripts:\nlauncher\\run.bat    # Windows\n./launcher/run.sh   # Linux/macOS\n\n# Or direct command:\npython -m uvicorn edge.app:app --host 127.0.0.1 --port 7860", code_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Step 6: Access Web Interface</b>", body_style))
    story.append(Preformatted("URL: http://127.0.0.1:7860\nUsername: admin", code_style))
    story.append(Spacer(1, 6))

    # Tests
    story.append(Paragraph("3. Verification Test Suite", h2_style))
    story.append(Preformatted("python -m pytest tests/unit -q --tb=short\npython -m tests.live.test_routes\npython -m tests.scenarios.test_alert_scenarios", code_style))

    doc.build(story)
    return True


def main():
    if generate_pdf_reportlab():
        print(f"[+] Generated PDF successfully: {PDF_PATH.name}")
    else:
        print(f"[i] Opening {HTML_PATH.name} in browser (Click 'Save / Print as PDF' or Ctrl+P)...")
        if HTML_PATH.exists():
            webbrowser.open(HTML_PATH.as_uri())


if __name__ == "__main__":
    main()
