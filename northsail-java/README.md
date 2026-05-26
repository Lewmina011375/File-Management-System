# FP Checking System (Java)

Java/Spring Boot port of the North Sails FP Checking Platform (originally Python/Flask).

## Requirements

- **Java 17+**
- **Maven 3.6+** (or use the Maven Wrapper if added)

## Build

```bash
mvn clean package -DskipTests
```

## Run

```bash
mvn spring-boot:run
```

Or run the JAR:

```bash
java -jar target/fp-checking-1.0.0.jar
```

The app listens on **http://localhost:5000** (same port as the Python version).

## Project layout

- `src/main/java/com/northsail/fpcheck/`
  - **controller/** – PageController (HTML), ApiController (REST)
  - **service/** – FileProcessorService, TxtParser, PdfService, WorkTicketPdfParser, TapingReportParser, WorkTicketExcelParser, ExcelReader, CsvReader, ExcelExport, CsvExport, ParseHelper
  - **config/** – AppConfig (upload path)
- `src/main/resources/`
  - **templates/** – index.html, battens_pocket.html, other_criteria.html (same UI as Python app)
  - **application.properties** – server port, upload dir, multipart limits

## API (same as Python)

- `POST /upload` – file + file_type (txt, taping, ticket)
- `POST /delete_file` – body: `{"file_type": "txt"|"taping"|"ticket"}`
- `POST /clear_uploads` – clear uploads folder and in-memory state
- `POST /run_checking` – body: `{"tolerance": 0.01}` → comparison results
- `GET /uploaded_status` – current files and parsed data
- `GET /download_report` – Excel comparison report
- `GET /other_criteria_json` – Other criteria table data
- `GET /batten_mapping_json` – Batten mapping for Battens & Pockets
- `GET /pocket_types_json` – Pocket types from taping report
- `GET /export_batten_mapping` – CSV batten mapping download

## Upload folder

Files are stored under `uploads/` (configurable via `app.upload-dir` in `application.properties`). The folder is cleared on startup.

## Optional: Tesseract OCR

The Python app can use Tesseract for work ticket PDFs when text extraction is poor. The Java build includes **Tess4J**; to use it you would need to:

1. Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (e.g. `C:\Program Files\Tesseract-OCR` on Windows).
2. Configure Tess4J or add OCR-based parsing in the service layer (not implemented in this port yet).

If you don’t need OCR, you can remove the `tess4j` dependency from `pom.xml` to avoid native library issues.

## Differences from Python version

- Parsing logic is simplified: same OE, DPI, Tier, measurements (Head, Luff, Leech, Foot, LP), and Excel/CSV strip count from `#` column. Batten/pocket lists and OCR are not fully ported; extend `TxtParser`, `WorkTicketPdfParser`, and related classes to match the Python behavior.
- Single in-memory state (no per-session storage); suitable for single-user or dev use.
