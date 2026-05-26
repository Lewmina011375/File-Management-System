package com.northsail.fpcheck.service;

import org.springframework.core.io.ByteArrayResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import jakarta.annotation.PostConstruct;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import java.util.stream.Collectors;

@Service
public class FileProcessorService {

    private static final Set<String> ALLOWED_EXTENSIONS = Set.of("txt", "pdf", "xlsx", "csv");
    private static final List<String> CRITERIA = List.of(
            "OE Number", "DPI", "Tier", "Head", "Luff", "Leech", "Foot", "LP");

    private final Path uploadPath;

    private final Map<String, Object> txtFile = new HashMap<>();
    private final Map<String, Object> tapingReport = new HashMap<>();
    private final Map<String, Object> workTicket = new HashMap<>();
    private final Map<String, String> fileNames = new HashMap<>();

    public FileProcessorService(Path uploadPath) {
        this.uploadPath = uploadPath;
    }

    @PostConstruct
    public void init() {
        clearUploadsFolder();
    }

    private void clearUploadsFolder() {
        if (!Files.isDirectory(uploadPath)) return;
        try {
            Files.list(uploadPath).filter(Files::isRegularFile).forEach(p -> {
                try { Files.delete(p); } catch (Exception ignored) {}
            });
        } catch (Exception ignored) {}
    }

    public ResponseEntity<?> upload(MultipartFile file, String fileType) {
        if (file == null || file.isEmpty())
            return ResponseEntity.badRequest().body(Map.of("error", "No selected file"));
        if (!Arrays.asList("txt", "taping", "ticket").contains(fileType))
            return ResponseEntity.badRequest().body(Map.of("error", "Invalid or missing file_type. Use: txt, taping, or ticket"));
        String originalFilename = file.getOriginalFilename();
        if (originalFilename == null || originalFilename.isEmpty())
            return ResponseEntity.badRequest().body(Map.of("error", "No selected file"));
        String ext = originalFilename.contains(".") ? originalFilename.substring(originalFilename.lastIndexOf('.') + 1).toLowerCase() : "";
        if (!ALLOWED_EXTENSIONS.contains(ext))
            return ResponseEntity.badRequest().body(Map.of("error", "File type not allowed"));

        String filename = sanitize(originalFilename);
        Path dest = uploadPath.resolve(filename);
        try {
            uploadPath.toFile().mkdirs();
            file.transferTo(dest.toFile());
        } catch (Exception e) {
            return ResponseEntity.internalServerError().body(Map.of("error", "Failed to save file"));
        }

        String key = fileType;
        String oldName = fileNames.get(key);
        if (oldName != null && !oldName.equals(filename)) {
            try { Files.deleteIfExists(uploadPath.resolve(oldName)); } catch (Exception ignored) {}
        }

        try {
            if ("txt".equals(fileType)) {
                txtFile.clear();
                txtFile.putAll(TxtParser.parse(dest));
                fileNames.put("txt", filename);
            } else if ("taping".equals(fileType)) {
                tapingReport.clear();
                tapingReport.putAll(TapingReportParser.parse(dest));
                fileNames.put("taping", filename);
            } else if ("ticket".equals(fileType)) {
                workTicket.clear();
                if (ext.equals("xlsx") || ext.equals("csv")) {
                    Map<String, Object> wt = WorkTicketExcelParser.parse(dest, ext);
                    workTicket.putAll(wt);
                } else {
                    workTicket.putAll(WorkTicketPdfParser.parse(dest));
                }
                fileNames.put("ticket", filename);
            }
            Map<String, Object> dataKey = "txt".equals(fileType) ? txtFile : "taping".equals(fileType) ? tapingReport : workTicket;
            return ResponseEntity.ok(Map.of("success", true, "filename", filename, "data", dataKey));
        } catch (Exception e) {
            try { Files.deleteIfExists(dest); } catch (Exception ignored) {}
            return ResponseEntity.internalServerError().body(Map.of("error", "Failed to process file. See server log."));
        }
    }

    public ResponseEntity<?> deleteFile(String fileType) {
        if (fileType == null) return ResponseEntity.badRequest().body(Map.of("error", "Invalid file type"));
        if ("txt".equals(fileType)) {
            String fn = fileNames.get("txt");
            txtFile.clear();
            fileNames.put("txt", null);
            if (fn != null) try { Files.deleteIfExists(uploadPath.resolve(fn)); } catch (Exception ignored) {}
        } else if ("taping".equals(fileType)) {
            String fn = fileNames.get("taping");
            tapingReport.clear();
            fileNames.put("taping", null);
            if (fn != null) try { Files.deleteIfExists(uploadPath.resolve(fn)); } catch (Exception ignored) {}
        } else if ("ticket".equals(fileType)) {
            String fn = fileNames.get("ticket");
            workTicket.clear();
            fileNames.put("ticket", null);
            if (fn != null) try { Files.deleteIfExists(uploadPath.resolve(fn)); } catch (Exception ignored) {}
        } else {
            return ResponseEntity.badRequest().body(Map.of("error", "Invalid file type"));
        }
        return ResponseEntity.ok(Map.of("success", true));
    }

    public ResponseEntity<?> clearUploads() {
        clearUploadsFolder();
        txtFile.clear();
        tapingReport.clear();
        workTicket.clear();
        fileNames.put("txt", null);
        fileNames.put("taping", null);
        fileNames.put("ticket", null);
        return ResponseEntity.ok(Map.of("success", true));
    }

    public ResponseEntity<?> runChecking(Map<String, Object> body) {
        double tolerance = 0.01;
        if (body != null && body.get("tolerance") != null) {
            try {
                tolerance = ((Number) body.get("tolerance")).doubleValue();
            } catch (Exception e) {
                return ResponseEntity.badRequest().body(Map.of("error", "tolerance must be a number"));
            }
        }
        if (tolerance < 0 || tolerance > 1)
            return ResponseEntity.badRequest().body(Map.of("error", "tolerance must be between 0 and 1"));
        List<Map<String, Object>> results = compareFiles(tolerance);
        return ResponseEntity.ok(results);
    }

    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> compareFiles(double tolerance) {
        List<Map<String, Object>> results = new ArrayList<>();
        for (String criterion : CRITERIA) {
            Object txtVal = txtFile.getOrDefault(criterion, "-");
            Object tapVal = tapingReport.getOrDefault(criterion, "-");
            Object wtVal = workTicket.getOrDefault(criterion, "-");
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("Criteria", criterion);
            row.put("Text_File", txtVal != null ? txtVal : "-");
            row.put("Taping_Report", tapVal != null ? tapVal : "-");
            row.put("Work_Ticket", wtVal != null ? wtVal : "-");
            row.put("Difference", "-");
            row.put("Status", "?");

            if ("OE Number".equals(criterion)) {
                List<Object> vals = Arrays.asList(txtVal, tapVal, wtVal);
                List<Object> nonDash = vals.stream().filter(v -> v != null && !"-".equals(v.toString())).collect(Collectors.toList());
                if (nonDash.size() >= 2) {
                    long distinct = nonDash.stream().map(Object::toString).distinct().count();
                    row.put("Status", distinct == 1 ? "✓" : "✗");
                }
            } else if (Set.of("Head", "Luff", "Leech", "Foot", "LP").contains(criterion)) {
                Double t = ParseHelper.toDouble(txtVal);
                Double w = ParseHelper.toDouble(wtVal);
                if (t == null || w == null) {
                    row.put("Difference", "INCOMPLETE");
                } else {
                    double diff = Math.round((w - t) * 1000) / 1000.0;
                    row.put("Difference", String.format("%.3f", diff));
                    row.put("Status", Math.abs(diff) <= tolerance ? "✓" : "✗");
                }
            } else {
                Double t = ParseHelper.toDouble(txtVal);
                Double tap = ParseHelper.toDouble(tapVal);
                Double w = ParseHelper.toDouble(wtVal);
                List<Double> available = new ArrayList<>();
                if (t != null) available.add(t);
                if (tap != null) available.add(tap);
                if (w != null) available.add(w);
                if (available.size() >= 2) {
                    double first = available.get(0);
                    boolean match = available.stream().allMatch(v -> Math.abs(v - first) <= tolerance);
                    row.put("Status", match ? "✓" : "✗");
                }
            }
            results.add(row);
        }
        return results;
    }

    public ResponseEntity<?> uploadedStatus() {
        Map<String, String> files = new HashMap<>();
        files.put("txt", fileNames.get("txt"));
        files.put("taping", fileNames.get("taping"));
        files.put("ticket", fileNames.get("ticket"));
        return ResponseEntity.ok(Map.of("files", files, "parsed", Map.of(
                "txt_file", new HashMap<>(txtFile),
                "taping_report", new HashMap<>(tapingReport),
                "work_ticket", new HashMap<>(workTicket))));
    }

    public ResponseEntity<Resource> downloadReport() {
        List<Map<String, Object>> results = compareFiles(0.01);
        String oe = (String) txtFile.get("OE Number");
        if (oe == null || "-".equals(oe)) oe = (String) tapingReport.get("OE Number");
        if (oe == null || "-".equals(oe)) oe = (String) workTicket.get("OE Number");
        String downloadName = (oe != null && !oe.isEmpty() && !"-".equals(oe)) ? oe + "_comparison_report.xlsx" : "comparison_report.xlsx";
        byte[] bytes = ExcelExport.toExcel(results);
        Resource resource = new ByteArrayResource(bytes);
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + downloadName + "\"")
                .body(resource);
    }

    public Map<String, Object> getOtherCriteriaJson() {
        Map<String, Object> out = new HashMap<>();
        out.put("strips_count_txt", txtFile.get("Strips_Count"));
        out.put("strips_count_tape", tapingReport.get("Strips_Count"));
        out.put("strips_count_wt", workTicket.get("Strips_Count"));
        out.put("strips_status", "-");
        out.put("spreader_txt", txtFile.get("Spreader_Patches"));
        out.put("spreader_tape", tapingReport.get("Spreader_Patches"));
        out.put("spreader_ticket", workTicket.get("Spreader_Patches"));
        out.put("spreader_status", "-");
        out.put("cunningham_txt", txtFile.get("Cunningham"));
        out.put("cunningham_tape", tapingReport.get("Cunningham"));
        out.put("cunningham_ticket", workTicket.get("Cunningham"));
        out.put("cunningham_status", "-");
        out.put("helix_txt", txtFile.get("Helix_Structure"));
        out.put("helix_tape", tapingReport.get("Helix_Structure"));
        out.put("helix_ticket", workTicket.get("Helix_Structure"));
        out.put("helix_status", "-");
        out.put("roller_txt", null);
        out.put("roller_tape", null);
        out.put("roller_ticket", null);
        out.put("roller_status", "-");
        return out;
    }

    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> getBattenMappingJson() {
        List<Integer> wtLengths = (List<Integer>) workTicket.get("Batten_Lengths_mm");
        List<Double> txtLengthsM = (List<Double>) txtFile.get("Batten_Lengths_m");
        if (wtLengths == null) wtLengths = Collections.emptyList();
        if (txtLengthsM == null) txtLengthsM = Collections.emptyList();
        List<String> txtList = (List<String>) txtFile.get("Batten_List");
        List<String> wtList = (List<String>) workTicket.get("Batten_List");
        if (txtList == null) txtList = Collections.emptyList();
        if (wtList == null) wtList = Collections.emptyList();
        List<Double> txtRev = new ArrayList<>(txtLengthsM);
        Collections.reverse(txtRev);
        int maxLen = Math.max(wtLengths.size(), txtRev.size());
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < maxLen; i++) {
            Integer wtLen = i < wtLengths.size() ? wtLengths.get(i) : null;
            Double txtM = i < txtRev.size() ? txtRev.get(i) : null;
            Integer txtMm = txtM != null ? (int) Math.round(txtM * 1000) : null;
            String wtType = i < wtList.size() ? wtList.get(i) : "";
            String txtType = i < txtList.size() ? txtList.get(i) : "";
            boolean match = wtLen != null && txtMm != null && Math.abs(wtLen - txtMm) <= 1;
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("WorkTicket_Row", i + 1);
            row.put("WorkTicket_Length_mm", wtLen != null ? wtLen : "");
            row.put("WorkTicket_Type", wtType);
            row.put("TextFile_Batten", i + 1);
            row.put("TextFile_Length_mm", txtMm != null ? txtMm : "");
            row.put("TextFile_Type", txtType);
            row.put("Match", match);
            rows.add(row);
        }
        return rows;
    }

    @SuppressWarnings("unchecked")
    public List<Map<String, String>> getPocketTypesJson() {
        List<Map<String, String>> out = new ArrayList<>();
        List<Map<String, Object>> list = (List<Map<String, Object>>) tapingReport.get("Pocket_List");
        if (list == null) return out;
        for (Map<String, Object> m : list) {
            Map<String, String> row = new HashMap<>();
            row.put("tape_layout", (String) m.getOrDefault("tape_layout", ""));
            row.put("pocket_count", (String) m.getOrDefault("pocket_count", ""));
            out.add(row);
        }
        return out;
    }

    public ResponseEntity<Resource> exportBattenMapping() {
        List<Map<String, Object>> rows = getBattenMappingJson();
        byte[] csv = CsvExport.toCsv(rows);
        Resource resource = new ByteArrayResource(csv);
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType("text/csv"))
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"batten_mapping.csv\"")
                .body(resource);
    }

    private static String sanitize(String name) {
        if (name == null) return "file";
        return name.replaceAll("[^a-zA-Z0-9._-]", "_");
    }
}
