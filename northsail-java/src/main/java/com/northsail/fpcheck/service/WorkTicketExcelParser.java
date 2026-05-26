package com.northsail.fpcheck.service;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/**
 * Parse work ticket from Excel or CSV (OE from filename, Strips_Count from # column if present).
 */
public final class WorkTicketExcelParser {

    public static Map<String, Object> parse(Path filepath, String ext) {
        Map<String, Object> data = new HashMap<>();
        try {
            String fileName = filepath.getFileName().toString();
            String oe = ParseHelper.getOe(fileName);
            if (oe != null) data.put("OE Number", oe);
            if ("xlsx".equals(ext)) {
                Map<String, Object> fromExcel = ExcelReader.readWorkTicket(filepath);
                data.putAll(fromExcel);
            } else if ("csv".equals(ext)) {
                Map<String, Object> fromCsv = CsvReader.readWorkTicket(filepath);
                data.putAll(fromCsv);
            }
        } catch (Exception e) {
            // log
        }
        return data;
    }
}
