package com.terevos.pdfreader

import android.net.Uri

data class PdfTab(
    val uri: Uri,
    val filename: String,
    var currentPage: Int = 0,
    var zoom: Float = 1.0f
)
