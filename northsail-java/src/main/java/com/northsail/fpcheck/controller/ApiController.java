package com.northsail.fpcheck.controller;

import com.northsail.fpcheck.service.FileProcessorService;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;
import java.util.Map;

@RestController
public class ApiController {

    private final FileProcessorService fileProcessorService;

    public ApiController(FileProcessorService fileProcessorService) {
        this.fileProcessorService = fileProcessorService;
    }

    @PostMapping("/upload")
    public ResponseEntity<?> upload(
            @RequestParam("file") MultipartFile file,
            @RequestParam("file_type") String fileType) {
        return fileProcessorService.upload(file, fileType);
    }

    @PostMapping("/delete_file")
    public ResponseEntity<?> deleteFile(@RequestBody Map<String, Object> body) {
        Object ft = body != null ? body.get("file_type") : null;
        return fileProcessorService.deleteFile(ft != null ? ft.toString() : null);
    }

    @PostMapping("/clear_uploads")
    public ResponseEntity<?> clearUploads() {
        return fileProcessorService.clearUploads();
    }

    @PostMapping("/run_checking")
    public ResponseEntity<?> runChecking(@RequestBody Map<String, Object> body) {
        return fileProcessorService.runChecking(body);
    }

    @GetMapping("/uploaded_status")
    public ResponseEntity<?> uploadedStatus() {
        return fileProcessorService.uploadedStatus();
    }

    @GetMapping("/download_report")
    public ResponseEntity<Resource> downloadReport() {
        return fileProcessorService.downloadReport();
    }

    @GetMapping("/other_criteria_json")
    public ResponseEntity<Map<String, Object>> otherCriteriaJson() {
        return ResponseEntity.ok(fileProcessorService.getOtherCriteriaJson());
    }

    @GetMapping("/batten_mapping_json")
    public ResponseEntity<List<Map<String, Object>>> battenMappingJson() {
        return ResponseEntity.ok(fileProcessorService.getBattenMappingJson());
    }

    @GetMapping("/pocket_types_json")
    public ResponseEntity<List<Map<String, String>>> pocketTypesJson() {
        return ResponseEntity.ok(fileProcessorService.getPocketTypesJson());
    }

    @GetMapping("/export_batten_mapping")
    public ResponseEntity<Resource> exportBattenMapping() {
        return fileProcessorService.exportBattenMapping();
    }
}
