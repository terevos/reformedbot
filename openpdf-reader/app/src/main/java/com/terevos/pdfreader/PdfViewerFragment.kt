package com.terevos.pdfreader

import android.app.AlertDialog
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.util.SizeF
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.Toast
import androidx.core.os.bundleOf
import androidx.fragment.app.Fragment
import com.github.barteksc.pdfviewer.listener.OnLoadCompleteListener
import com.github.barteksc.pdfviewer.listener.OnPageChangeListener
import com.github.barteksc.pdfviewer.listener.OnRenderListener
import com.github.barteksc.pdfviewer.scroll.DefaultScrollHandle
import com.github.barteksc.pdfviewer.util.FitPolicy
import com.terevos.pdfreader.annotation.AnnotationManager
import com.terevos.pdfreader.annotation.AnnotationType
import com.terevos.pdfreader.annotation.PdfAnnotation
import com.terevos.pdfreader.databinding.FragmentPdfViewerBinding
import com.terevos.pdfreader.util.FileUtils
import com.terevos.pdfreader.util.PdfCoordinateMapper
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

class PdfViewerFragment : Fragment() {

    private var _binding: FragmentPdfViewerBinding? = null
    private val binding get() = _binding!!

    private val annotationManager = AnnotationManager()
    private var coordinateMapper: PdfCoordinateMapper? = null
    private var cachedFile: File? = null
    private var isPageMode = false   // false = smooth vertical, true = page-by-page horizontal
    private var currentPage = 0

    companion object {
        private const val ARG_URI      = "uri"
        private const val ARG_FILENAME = "filename"

        fun newInstance(uri: Uri, filename: String) = PdfViewerFragment().apply {
            arguments = bundleOf(ARG_URI to uri.toString(), ARG_FILENAME to filename)
        }
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View {
        _binding = FragmentPdfViewerBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        val uriString = arguments?.getString(ARG_URI)      ?: return
        val filename  = arguments?.getString(ARG_FILENAME) ?: "document.pdf"
        setupAnnotationOverlay()
        setupToolbar()
        loadPdf(Uri.parse(uriString), filename)
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    // ── PDF loading ───────────────────────────────────────────────────────────

    private fun loadPdf(uri: Uri, filename: String) {
        CoroutineScope(Dispatchers.IO).launch {
            val file = FileUtils.copyToCache(requireContext(), uri, filename)
            cachedFile = file
            annotationManager.loadFromPdf(file)
            withContext(Dispatchers.Main) { configurePdfView(file) }
        }
    }

    private fun configurePdfView(file: File) {
        binding.pdfView
            .fromFile(file)
            .defaultPage(currentPage)
            .enableSwipe(true)
            .swipeHorizontal(isPageMode)
            .pageSnap(isPageMode)
            .pageFling(isPageMode)
            .fitEachPage(isPageMode)
            .enableDoubletap(true)
            .enableAnnotationRendering(true)
            .scrollHandle(DefaultScrollHandle(requireContext()))
            .spacing(PdfCoordinateMapper.PAGE_SPACING_PX.toInt())
            .pageFitPolicy(FitPolicy.WIDTH)
            .onLoad(OnLoadCompleteListener { pageCount ->
                val mapper = PdfCoordinateMapper(binding.pdfView)
                // Supply page sizes so the mapper can compute screen positions
                val sizes = (0 until pageCount).associate { i ->
                    i to binding.pdfView.getPageSize(i).let { s ->
                        SizeF(s.width, s.height)
                    }
                }
                mapper.setPageSizes(sizes)
                coordinateMapper = mapper
                binding.annotationOverlay.coordinateMapper = mapper
                binding.annotationOverlay.annotationManager = annotationManager
                binding.annotationOverlay.invalidate()
            })
            .onPageChange(OnPageChangeListener { page, _ ->
                currentPage = page
                binding.annotationOverlay.currentPage = page
                binding.annotationOverlay.invalidate()
            })
            .onRender(OnRenderListener { _ ->
                binding.annotationOverlay.invalidate()
            })
            .load()
    }

    // ── annotation overlay wiring ─────────────────────────────────────────────

    private fun setupAnnotationOverlay() {
        binding.annotationOverlay.onAnnotationAdded = {
            binding.annotationOverlay.invalidate()
        }
        binding.annotationOverlay.onTextAnnotationRequested = { pdfL, pdfB, pdfR, pdfT, page ->
            showTextAnnotationDialog(pdfL, pdfB, pdfR, pdfT, page)
        }
    }

    // ── toolbar ───────────────────────────────────────────────────────────────

    private fun setupToolbar() {
        binding.btnPointer.setOnClickListener       { setTool(AnnotationType.NONE) }
        binding.btnHighlight.setOnClickListener     { setTool(AnnotationType.HIGHLIGHT) }
        binding.btnUnderline.setOnClickListener     { setTool(AnnotationType.UNDERLINE) }
        binding.btnStrikethrough.setOnClickListener { setTool(AnnotationType.STRIKETHROUGH) }
        binding.btnInk.setOnClickListener           { setTool(AnnotationType.INK) }
        binding.btnText.setOnClickListener          { setTool(AnnotationType.TEXT) }

        binding.btnScrollMode.setOnClickListener { toggleScrollMode() }
        binding.btnZoomIn.setOnClickListener  {
            binding.pdfView.zoomWithAnimation(binding.pdfView.zoom * 1.25f)
        }
        binding.btnZoomOut.setOnClickListener {
            binding.pdfView.zoomWithAnimation(binding.pdfView.zoom * 0.8f)
        }
        binding.btnSave.setOnClickListener { saveAnnotations() }
    }

    private fun setTool(tool: AnnotationType) {
        binding.annotationOverlay.activeTool = tool
        // Highlight the active button
        val buttons = listOf(binding.btnPointer, binding.btnHighlight, binding.btnUnderline,
                             binding.btnStrikethrough, binding.btnInk, binding.btnText)
        val tools   = listOf(AnnotationType.NONE, AnnotationType.HIGHLIGHT, AnnotationType.UNDERLINE,
                             AnnotationType.STRIKETHROUGH, AnnotationType.INK, AnnotationType.TEXT)
        buttons.forEachIndexed { i, btn -> btn.isSelected = tools[i] == tool }
    }

    private fun toggleScrollMode() {
        isPageMode = !isPageMode
        binding.btnScrollMode.text =
            if (isPageMode) getString(R.string.mode_smooth) else getString(R.string.mode_pages)
        cachedFile?.let { configurePdfView(it) }
    }

    // ── text annotation dialog ────────────────────────────────────────────────

    private fun showTextAnnotationDialog(
        pdfL: Float, pdfB: Float, pdfR: Float, pdfT: Float, pageIndex: Int
    ) {
        val input = EditText(requireContext()).apply { hint = "Annotation text" }
        AlertDialog.Builder(requireContext())
            .setTitle("Add Text Annotation")
            .setView(input)
            .setPositiveButton("Add") { _, _ ->
                val text = input.text.toString().trim()
                if (text.isNotEmpty()) {
                    val ann = PdfAnnotation(AnnotationType.TEXT, pageIndex,
                                           pdfL, pdfB, pdfR, pdfT,
                                           Color.DKGRAY, content = text)
                    annotationManager.addAnnotation(ann)
                    binding.annotationOverlay.invalidate()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ── save ──────────────────────────────────────────────────────────────────

    private fun saveAnnotations() {
        val file = cachedFile ?: return
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val tmp = File(requireContext().cacheDir, "annotated_${file.name}")
                annotationManager.saveToPdf(file, tmp)
                tmp.copyTo(file, overwrite = true)
                tmp.delete()
                // Reload embedded annotations
                annotationManager.loadFromPdf(file)
                withContext(Dispatchers.Main) {
                    Toast.makeText(requireContext(), "Annotations saved", Toast.LENGTH_SHORT).show()
                    configurePdfView(file)
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(requireContext(), "Save failed: ${e.message}",
                                   Toast.LENGTH_LONG).show()
                }
            }
        }
    }
}
