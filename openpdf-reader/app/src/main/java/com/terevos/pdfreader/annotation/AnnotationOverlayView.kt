package com.terevos.pdfreader.annotation

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PointF
import android.graphics.RectF
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import com.terevos.pdfreader.util.PdfCoordinateMapper

/**
 * Transparent view layered directly on top of PDFView.
 *
 * Responsibilities:
 *  - Draw saved annotations for the currently visible page
 *  - Capture touch gestures to create new annotations
 *  - Delegate touch to the PDFView (scroll/zoom) when no tool is active
 */
class AnnotationOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    var annotationManager: AnnotationManager? = null
    var coordinateMapper: PdfCoordinateMapper? = null
    var activeTool: AnnotationType = AnnotationType.NONE
    var currentPage: Int = 0

    /** Called when a new annotation has been finalised. */
    var onAnnotationAdded: ((PdfAnnotation) -> Unit)? = null

    /**
     * Called when the user taps with the TEXT tool.
     * Provides the tap location in PDF coordinates so the fragment can show a dialog.
     */
    var onTextAnnotationRequested: ((pdfLeft: Float, pdfBottom: Float,
                                     pdfRight: Float, pdfTop: Float,
                                     pageIndex: Int) -> Unit)? = null

    // ── paints ────────────────────────────────────────────────────────────────

    private val highlightPaint = Paint().apply {
        color = Color.argb(100, 255, 230, 0)
        style = Paint.Style.FILL
    }
    private val underlinePaint = Paint().apply {
        color = Color.BLUE
        style = Paint.Style.STROKE
        strokeWidth = 3f
    }
    private val strikethroughPaint = Paint().apply {
        color = Color.RED
        style = Paint.Style.STROKE
        strokeWidth = 3f
    }
    private val inkPaint = Paint().apply {
        color = Color.BLACK
        style = Paint.Style.STROKE
        strokeWidth = 5f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
        isAntiAlias = true
    }
    private val textPaint = Paint().apply {
        color = Color.DKGRAY
        textSize = 36f
        isAntiAlias = true
    }
    private val selectionPaint = Paint().apply {
        color = Color.argb(60, 0, 120, 255)
        style = Paint.Style.FILL
    }

    // ── in-progress gesture state ─────────────────────────────────────────────

    private var startX = 0f
    private var startY = 0f
    private val currentRect = RectF()
    private val currentInkPath = Path()
    private val currentInkScreenPoints = mutableListOf<PointF>()

    // ── touch ─────────────────────────────────────────────────────────────────

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (activeTool == AnnotationType.NONE) return false   // let PDFView handle

        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                startX = event.x
                startY = event.y
                currentRect.set(startX, startY, startX, startY)
                if (activeTool == AnnotationType.INK) {
                    currentInkPath.reset()
                    currentInkScreenPoints.clear()
                    currentInkPath.moveTo(event.x, event.y)
                    currentInkScreenPoints.add(PointF(event.x, event.y))
                }
                return true
            }
            MotionEvent.ACTION_MOVE -> {
                updateCurrentRect(event.x, event.y)
                if (activeTool == AnnotationType.INK) {
                    currentInkPath.lineTo(event.x, event.y)
                    currentInkScreenPoints.add(PointF(event.x, event.y))
                }
                invalidate()
                return true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                updateCurrentRect(event.x, event.y)
                finaliseAnnotation(event.x, event.y)
                invalidate()
                return true
            }
        }
        return false
    }

    private fun updateCurrentRect(x: Float, y: Float) {
        currentRect.set(
            minOf(startX, x), minOf(startY, y),
            maxOf(startX, x), maxOf(startY, y)
        )
    }

    private fun finaliseAnnotation(upX: Float, upY: Float) {
        val mapper = coordinateMapper
        when (activeTool) {
            AnnotationType.HIGHLIGHT,
            AnnotationType.UNDERLINE,
            AnnotationType.STRIKETHROUGH -> {
                if (currentRect.width() > 8f || currentRect.height() > 8f) {
                    val pdf = mapper?.screenRectToPdf(currentRect, currentPage)
                    if (pdf != null) {
                        val color = when (activeTool) {
                            AnnotationType.HIGHLIGHT     -> Color.argb(180, 255, 230, 0)
                            AnnotationType.UNDERLINE     -> Color.BLUE
                            AnnotationType.STRIKETHROUGH -> Color.RED
                            else                         -> Color.YELLOW
                        }
                        val ann = PdfAnnotation(activeTool, currentPage,
                            pdf.left, pdf.bottom, pdf.right, pdf.top, color)
                        annotationManager?.addAnnotation(ann)
                        onAnnotationAdded?.invoke(ann)
                    }
                }
                currentRect.setEmpty()
            }
            AnnotationType.INK -> {
                if (currentInkScreenPoints.size > 1) {
                    val pdfStrokes = mapper?.let { m ->
                        listOf(m.screenPointsToPdf(currentInkScreenPoints, currentPage))
                    } ?: listOf(currentInkScreenPoints.map { PointF(it.x, it.y) })

                    val bounds = RectF()
                    currentInkPath.computeBounds(bounds, true)
                    val pdf = mapper?.screenRectToPdf(bounds, currentPage)

                    val ann = PdfAnnotation(
                        AnnotationType.INK, currentPage,
                        pdf?.left ?: bounds.left, pdf?.bottom ?: bounds.top,
                        pdf?.right ?: bounds.right, pdf?.top ?: bounds.bottom,
                        Color.BLACK, inkStrokes = pdfStrokes
                    )
                    annotationManager?.addAnnotation(ann)
                    onAnnotationAdded?.invoke(ann)
                }
                currentInkPath.reset()
                currentInkScreenPoints.clear()
                currentRect.setEmpty()
            }
            AnnotationType.TEXT -> {
                val tapRect = RectF(upX - 4f, upY - 20f, upX + 200f, upY + 20f)
                val pdf = mapper?.screenRectToPdf(tapRect, currentPage)
                onTextAnnotationRequested?.invoke(
                    pdf?.left   ?: tapRect.left,
                    pdf?.bottom ?: tapRect.top,
                    pdf?.right  ?: tapRect.right,
                    pdf?.top    ?: tapRect.bottom,
                    currentPage
                )
                currentRect.setEmpty()
            }
            AnnotationType.NONE -> Unit
        }
    }

    // ── drawing ───────────────────────────────────────────────────────────────

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val manager = annotationManager ?: return

        // Draw saved annotations for the current page
        for (ann in manager.getAnnotations(currentPage)) {
            val sr = coordinateMapper?.pdfRectToScreen(
                ann.pdfLeft, ann.pdfBottom, ann.pdfRight, ann.pdfTop, currentPage
            ) ?: continue

            when (ann.type) {
                AnnotationType.HIGHLIGHT ->
                    canvas.drawRect(sr, highlightPaint)

                AnnotationType.UNDERLINE ->
                    canvas.drawLine(sr.left, sr.bottom, sr.right, sr.bottom, underlinePaint)

                AnnotationType.STRIKETHROUGH -> {
                    val midY = sr.centerY()
                    canvas.drawLine(sr.left, midY, sr.right, midY, strikethroughPaint)
                }

                AnnotationType.INK -> {
                    for (stroke in ann.inkStrokes) {
                        if (stroke.isEmpty()) continue
                        val path = Path()
                        val first = coordinateMapper?.pdfRectToScreen(
                            stroke[0].x, stroke[0].y, stroke[0].x, stroke[0].y, currentPage
                        ) ?: continue
                        path.moveTo(first.left, first.top)
                        for (i in 1 until stroke.size) {
                            val s = coordinateMapper?.pdfRectToScreen(
                                stroke[i].x, stroke[i].y, stroke[i].x, stroke[i].y, currentPage
                            ) ?: continue
                            path.lineTo(s.left, s.top)
                        }
                        canvas.drawPath(path, inkPaint)
                    }
                }

                AnnotationType.TEXT ->
                    canvas.drawText(ann.content, sr.left, sr.top, textPaint)

                AnnotationType.NONE -> Unit
            }
        }

        // Draw in-progress gesture feedback
        when (activeTool) {
            AnnotationType.HIGHLIGHT ->
                if (!currentRect.isEmpty) canvas.drawRect(currentRect, selectionPaint)

            AnnotationType.UNDERLINE ->
                if (!currentRect.isEmpty)
                    canvas.drawLine(currentRect.left, currentRect.bottom,
                                    currentRect.right, currentRect.bottom, underlinePaint)

            AnnotationType.STRIKETHROUGH ->
                if (!currentRect.isEmpty) {
                    val midY = currentRect.centerY()
                    canvas.drawLine(currentRect.left, midY, currentRect.right, midY, strikethroughPaint)
                }

            AnnotationType.INK ->
                if (!currentInkPath.isEmpty) canvas.drawPath(currentInkPath, inkPaint)

            else -> Unit
        }
    }
}
