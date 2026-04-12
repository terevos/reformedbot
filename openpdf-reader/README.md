# OpenPDF Reader

An open-source PDF reader for Android with multi-tab support and full annotation capabilities.

## Features

- **Multiple tabs** — open and switch between several PDFs simultaneously
- **Zoom** — pinch-to-zoom and +/− buttons
- **Scroll modes** — toggle between smooth (continuous) scrolling and page-by-page navigation
- **Annotations** — all annotations are saved back into the PDF file:
  - Highlight
  - Underline
  - Strikethrough
  - Free-draw / ink
  - Text (free-text annotation)

## Requirements

- Android 5.0+ (API 21)
- Android Studio Hedgehog or newer (for building)

## Building

```bash
# Generate the Gradle wrapper (first time only)
gradle wrapper --gradle-version 8.2

# Build a debug APK
./gradlew assembleDebug
```

Or simply open the project in Android Studio and click **Run**.

## Architecture

| Layer | Files |
|---|---|
| UI | `MainActivity`, `TabAdapter`, `PdfViewerFragment` |
| Annotation overlay | `AnnotationOverlayView` |
| Annotation persistence | `AnnotationManager` (PDFBox-Android) |
| Coordinate mapping | `PdfCoordinateMapper` |
| Data model | `PdfTab`, `PdfAnnotation`, `AnnotationType` |

## Dependencies

| Library | Purpose | License |
|---|---|---|
| [AndroidPdfViewer](https://github.com/DImuthuUpe/AndroidPdfViewer) | PDF rendering (PDFium) | Apache 2.0 |
| [PDFBox-Android](https://github.com/TomRoush/PdfBox-Android) | Annotation read/write | Apache 2.0 |
| Material 3 / AndroidX | UI components | Apache 2.0 |

## License

**GNU General Public License v3.0** — see [LICENSE](LICENSE).

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.
