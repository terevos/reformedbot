package com.terevos.pdfreader.annotation

import android.graphics.Color
import android.graphics.PointF
import com.tom_roush.pdfbox.pdmodel.PDDocument
import com.tom_roush.pdfbox.pdmodel.common.PDRectangle
import com.tom_roush.pdfbox.pdmodel.graphics.color.PDColor
import com.tom_roush.pdfbox.pdmodel.graphics.color.PDDeviceRGB
import com.tom_roush.pdfbox.pdmodel.interactive.annotation.PDAnnotationFreeText
import com.tom_roush.pdfbox.pdmodel.interactive.annotation.PDAnnotationInk
import com.tom_roush.pdfbox.pdmodel.interactive.annotation.PDAnnotationTextMarkup
import java.io.File

class AnnotationManager {

    // page index → list of annotations
    private val store: MutableMap<Int, MutableList<PdfAnnotation>> = mutableMapOf()

    fun getAnnotations(pageIndex: Int): List<PdfAnnotation> =
        store[pageIndex] ?: emptyList()

    fun addAnnotation(annotation: PdfAnnotation) {
        store.getOrPut(annotation.pageIndex) { mutableListOf() }.add(annotation)
    }

    fun removeAnnotation(annotation: PdfAnnotation) {
        store[annotation.pageIndex]?.remove(annotation)
    }

    fun clearAll() = store.clear()

    // -------------------------------------------------------------------------
    // Load existing annotations from a PDF file
    // -------------------------------------------------------------------------

    fun loadFromPdf(file: File) {
        store.clear()
        try {
            PDDocument.load(file).use { doc ->
                for (pageIndex in 0 until doc.numberOfPages) {
                    val page = doc.getPage(pageIndex)
                    for (pdAnn in page.annotations ?: continue) {
                        val rect = pdAnn.rectangle ?: continue
                        val color = pdAnn.color?.toAndroidColor() ?: Color.YELLOW

                        when (pdAnn) {
                            is PDAnnotationTextMarkup -> {
                                val type = when (pdAnn.subType) {
                                    PDAnnotationTextMarkup.SUB_TYPE_HIGHLIGHT  -> AnnotationType.HIGHLIGHT
                                    PDAnnotationTextMarkup.SUB_TYPE_UNDERLINE  -> AnnotationType.UNDERLINE
                                    PDAnnotationTextMarkup.SUB_TYPE_STRIKEOUT  -> AnnotationType.STRIKETHROUGH
                                    else -> continue
                                }
                                store.getOrPut(pageIndex) { mutableListOf() }.add(
                                    PdfAnnotation(type, pageIndex,
                                        rect.lowerLeftX, rect.lowerLeftY,
                                        rect.upperRightX, rect.upperRightY,
                                        color)
                                )
                            }
                            is PDAnnotationInk -> {
                                val strokes = (pdAnn.inkList ?: emptyArray()).map { stroke ->
                                    val pts = mutableListOf<PointF>()
                                    var i = 0
                                    while (i + 1 < stroke.size) {
                                        pts.add(PointF(stroke[i], stroke[i + 1]))
                                        i += 2
                                    }
                                    pts as List<PointF>
                                }
                                store.getOrPut(pageIndex) { mutableListOf() }.add(
                                    PdfAnnotation(AnnotationType.INK, pageIndex,
                                        rect.lowerLeftX, rect.lowerLeftY,
                                        rect.upperRightX, rect.upperRightY,
                                        color, inkStrokes = strokes)
                                )
                            }
                            is PDAnnotationFreeText -> {
                                store.getOrPut(pageIndex) { mutableListOf() }.add(
                                    PdfAnnotation(AnnotationType.TEXT, pageIndex,
                                        rect.lowerLeftX, rect.lowerLeftY,
                                        rect.upperRightX, rect.upperRightY,
                                        color, content = pdAnn.contents ?: "")
                                )
                            }
                        }
                    }
                }
            }
        } catch (_: Exception) {
            // Unreadable or annotation-less file — silently ignore
        }
    }

    // -------------------------------------------------------------------------
    // Save all in-memory annotations into the PDF, writing to outputFile
    // -------------------------------------------------------------------------

    fun saveToPdf(sourceFile: File, outputFile: File) {
        PDDocument.load(sourceFile).use { doc ->
            for ((pageIndex, annotations) in store) {
                if (pageIndex >= doc.numberOfPages) continue
                val page = doc.getPage(pageIndex)

                for (ann in annotations) {
                    val pdRect = PDRectangle(ann.pdfLeft, ann.pdfBottom,
                                             ann.pdfRight - ann.pdfLeft,
                                             ann.pdfTop   - ann.pdfBottom)
                    val pdColor = ann.color.toPdColor()

                    when (ann.type) {
                        AnnotationType.HIGHLIGHT -> {
                            PDAnnotationTextMarkup(PDAnnotationTextMarkup.SUB_TYPE_HIGHLIGHT).also {
                                it.rectangle = pdRect
                                it.color = pdColor
                                it.setQuadPoints(pdRect.toQuadPoints())
                                page.annotations.add(it)
                            }
                        }
                        AnnotationType.UNDERLINE -> {
                            PDAnnotationTextMarkup(PDAnnotationTextMarkup.SUB_TYPE_UNDERLINE).also {
                                it.rectangle = pdRect
                                it.color = pdColor
                                it.setQuadPoints(pdRect.toQuadPoints())
                                page.annotations.add(it)
                            }
                        }
                        AnnotationType.STRIKETHROUGH -> {
                            PDAnnotationTextMarkup(PDAnnotationTextMarkup.SUB_TYPE_STRIKEOUT).also {
                                it.rectangle = pdRect
                                it.color = pdColor
                                it.setQuadPoints(pdRect.toQuadPoints())
                                page.annotations.add(it)
                            }
                        }
                        AnnotationType.INK -> {
                            PDAnnotationInk().also {
                                it.rectangle = pdRect
                                it.color = pdColor
                                it.inkList = ann.inkStrokes.map { stroke ->
                                    FloatArray(stroke.size * 2) { idx ->
                                        if (idx % 2 == 0) stroke[idx / 2].x else stroke[idx / 2].y
                                    }
                                }.toTypedArray()
                                page.annotations.add(it)
                            }
                        }
                        AnnotationType.TEXT -> {
                            PDAnnotationFreeText().also {
                                it.rectangle = pdRect
                                it.color = pdColor
                                it.contents = ann.content
                                it.defaultAppearance = "/Helv 12 Tf 0 0 0 rg"
                                page.annotations.add(it)
                            }
                        }
                        AnnotationType.NONE -> Unit
                    }
                }
            }
            doc.save(outputFile)
        }
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private fun PDColor.toAndroidColor(): Int {
        val c = components
        return if (c.size >= 3)
            Color.rgb((c[0] * 255).toInt(), (c[1] * 255).toInt(), (c[2] * 255).toInt())
        else Color.YELLOW
    }

    private fun Int.toPdColor(): PDColor {
        return PDColor(
            floatArrayOf(Color.red(this) / 255f,
                         Color.green(this) / 255f,
                         Color.blue(this) / 255f),
            PDDeviceRGB.INSTANCE
        )
    }

    /**
     * QuadPoints for a rectangular markup: TL, TR, BL, BR in PDF space.
     * PDRectangle: lowerLeftX/Y → upperRightX/Y
     */
    private fun PDRectangle.toQuadPoints(): FloatArray = floatArrayOf(
        lowerLeftX,  upperRightY,   // top-left
        upperRightX, upperRightY,   // top-right
        lowerLeftX,  lowerLeftY,    // bottom-left
        upperRightX, lowerLeftY     // bottom-right
    )
}
