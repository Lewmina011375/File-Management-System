package com.northsail.fpcheck.service;

import java.io.ByteArrayOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

public final class CsvExport {

    public static byte[] toCsv(List<Map<String, Object>> rows) {
        if (rows.isEmpty()) return "".getBytes(StandardCharsets.UTF_8);
        String[] headers = {"WorkTicket_Row", "WorkTicket_Length_mm", "WorkTicket_Type", "TextFile_Batten", "TextFile_Length_mm", "TextFile_Type", "Match"};
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (OutputStreamWriter w = new OutputStreamWriter(out, StandardCharsets.UTF_8)) {
            w.write(String.join(",", headers));
            w.write("\n");
            for (Map<String, Object> row : rows) {
                String[] vals = new String[headers.length];
                for (int i = 0; i < headers.length; i++) {
                    Object v = row.get(headers[i]);
                    vals[i] = v != null ? escapeCsv(v.toString()) : "";
                }
                w.write(String.join(",", vals));
                w.write("\n");
            }
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
        return out.toByteArray();
    }

    private static String escapeCsv(String s) {
        if (s == null) return "";
        if (s.contains(",") || s.contains("\"") || s.contains("\n")) {
            return "\"" + s.replace("\"", "\"\"") + "\"";
        }
        return s;
    }
}
