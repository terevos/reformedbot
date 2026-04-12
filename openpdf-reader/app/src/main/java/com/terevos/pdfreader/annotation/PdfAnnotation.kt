package com.terevos.pdfreader.annotation

import android.graphics.PointF

/**
 * Represents a single annotation on a PDF page.
 *
 * Coordinates are stored in PDF page space:
 *   - Origin at bottom-left of page
 *   - Y increases upward
 *   - Units are PDF points
 *
 * Fields:
 *   pdfLeft/pdfRight   — horizontal extent (left < right)
 *   pdfBottom/pdfTop   — vertical extent (bottom < top, PDF convention)
 */
data class PdfAnnotation(
    val type: AnnotationType,
    val pageIndex: Int,
    val pdfLeft: Float,
    val pdfBottom: Float,
    val pdfRight: Float,
    val pdfTop: Float,
    val color: Int,
    val content: String = "",                         // TEXT annotations
    val inkStrokes: List<List<PointF>> = emptyList()  // INK annotations; points in PDF space
)
