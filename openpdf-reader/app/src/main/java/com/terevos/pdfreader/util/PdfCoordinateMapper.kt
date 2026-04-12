package com.terevos.pdfreader.util

import android.graphics.PointF
import android.graphics.RectF
import android.util.SizeF
import com.github.barteksc.pdfviewer.PDFView

/**
 * Maps between screen coordinates (pixels, top-left origin) and PDF page coordinates
 * (PDF points, bottom-left origin, Y increases upward).
 *
 * Pages are assumed to be laid out vertically (default scroll direction).
 * Call [setPageSizes] once the PDF has loaded, then call [invalidate] after
 * scroll/zoom changes so subsequent conversions use fresh offsets.
 */
class PdfCoordinateMapper(private val pdfView: PDFView) {

    // PDF page dimensions in PDF points, indexed by page number
    private val pageSizes = mutableMapOf<Int, SizeF>()

    /** Supply page dimensions (in PDF points) after the document loads. */
    fun setPageSizes(sizes: Map<Int, SizeF>) {
        pageSizes.clear()
        pageSizes.putAll(sizes)
    }

    /**
     * Returns the on-screen bounding rect of [pageIndex] at the current zoom/scroll,
     * relative to the top-left of the PDFView widget.
     */
    private fun pageScreenBounds(pageIndex: Int): RectF? {
        val ps = pageSizes[pageIndex] ?: return null
        val zoom = pdfView.zoom

        // Scale factor so that the page fits the view width at zoom = 1.
        val baseScale = if (ps.width > 0f) pdfView.width.toFloat() / ps.width else 1f
        val renderedW = ps.width * baseScale * zoom
        val renderedH = ps.height * baseScale * zoom

        // Accumulate Y offset of this page in content space (pages stacked top-to-bottom).
        var contentY = 0f
        for (i in 0 until pageIndex) {
            val s = pageSizes[i] ?: continue
            val bs = if (s.width > 0f) pdfView.width.toFloat() / s.width else 1f
            contentY += s.height * bs * zoom + PAGE_SPACING_PX * zoom
        }

        // pdfView.currentXOffset / currentYOffset: canvas translation applied by PDFView.
        // Screen position = content position + offset (offset is <= 0 when scrolled).
        val screenLeft = pdfView.currentXOffset  // content X = 0 for full-width pages
        val screenTop = contentY + pdfView.currentYOffset

        return RectF(screenLeft, screenTop, screenLeft + renderedW, screenTop + renderedH)
    }

    /**
     * Convert a rectangle in screen-pixel space into PDF page coordinates for [pageIndex].
     * Returns null if page size is unavailable.
     */
    fun screenRectToPdf(screenRect: RectF, pageIndex: Int): PdfRect? {
        val bounds = pageScreenBounds(pageIndex) ?: return null
        val ps = pageSizes[pageIndex] ?: return null
        if (bounds.width() == 0f || bounds.height() == 0f) return null

        val relLeft   = (screenRect.left   - bounds.left) / bounds.width()
        val relRight  = (screenRect.right  - bounds.left) / bounds.width()
        val relTop    = (screenRect.top    - bounds.top)  / bounds.height()
        val relBottom = (screenRect.bottom - bounds.top)  / bounds.height()

        // Flip Y: relTop (near 0) → near pdfTop; relBottom (near 1) → near 0
        return PdfRect(
            left   = relLeft   * ps.width,
            bottom = (1f - relBottom) * ps.height,
            right  = relRight  * ps.width,
            top    = (1f - relTop)    * ps.height
        )
    }

    /**
     * Convert a PDF page rectangle back into screen-pixel coordinates.
     * Returns null if page size is unavailable.
     */
    fun pdfRectToScreen(pdfLeft: Float, pdfBottom: Float, pdfRight: Float, pdfTop: Float,
                        pageIndex: Int): RectF? {
        val bounds = pageScreenBounds(pageIndex) ?: return null
        val ps = pageSizes[pageIndex] ?: return null
        if (ps.width == 0f || ps.height == 0f) return null

        val screenLeft   = bounds.left + (pdfLeft  / ps.width)  * bounds.width()
        val screenRight  = bounds.left + (pdfRight / ps.width)  * bounds.width()
        // Flip Y back
        val screenTop    = bounds.top  + (1f - pdfTop    / ps.height) * bounds.height()
        val screenBottom = bounds.top  + (1f - pdfBottom / ps.height) * bounds.height()

        return RectF(screenLeft, screenTop, screenRight, screenBottom)
    }

    /** Convert a list of screen-space points to PDF page coordinates for [pageIndex]. */
    fun screenPointsToPdf(points: List<PointF>, pageIndex: Int): List<PointF> {
        return points.mapNotNull { pt ->
            val r = screenRectToPdf(RectF(pt.x, pt.y, pt.x, pt.y), pageIndex) ?: return@mapNotNull null
            PointF(r.left, r.top)
        }
    }

    /** Simple holder for a rectangle in PDF coordinate space. */
    data class PdfRect(val left: Float, val bottom: Float, val right: Float, val top: Float)

    companion object {
        /** Page gap in pixels (should match the `spacing()` value passed to PDFView). */
        const val PAGE_SPACING_PX = 8f
    }
}
