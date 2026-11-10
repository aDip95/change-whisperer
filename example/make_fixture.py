#!/usr/bin/env python3
"""
Genera il PDF di esempio usato dalla test suite (`example/sample_graduatoria.pdf`).

Il PDF contiene una tabella fittizia con le colonne POSIZIONE e
CODICE MECCANOGRAFICO che il bot sa analizzare. I codici sono finti
(SAMPLE0001..SAMPLE0010) e i nominativi sono segnaposto: nessun dato reale.

Richiede reportlab (solo per rigenerare la fixture, non a runtime):
    pip install reportlab
    python example/make_fixture.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

OUT = Path(__file__).with_name("sample_graduatoria.pdf")

# Posizione → (codice meccanografico fittizio, cognome, nome) segnaposto.
ROWS = [
    (str(i), f"SAMPLE{i:04d}", f"Cognome{i:02d}", f"Nome{i:02d}")
    for i in range(1, 11)
]


def build():
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(OUT), pagesize=A4)
    elements = [
        Paragraph("Graduatoria di esempio (dati fittizi)", styles["Title"]),
        Spacer(1, 12),
        Paragraph(
            "Documento generato per i test automatici. Tutti i codici e "
            "i nominativi sono inventati.",
            styles["Normal"],
        ),
        Spacer(1, 18),
    ]

    data = [["POSIZIONE", "CODICE MECCANOGRAFICO", "COGNOME", "NOME"], *ROWS]
    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elements.append(table)

    doc.build(elements)
    print(f"Scritto {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
