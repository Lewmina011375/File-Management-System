package com.northsail.fpcheck.service;

import com.opencsv.CSVReader;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class CsvReader {

    public static Map<String, Object> readWorkTicket(Path path) {
        Map<String, Object> data = new HashMap<>();
        try (var reader = new CSVReader(Files.newBufferedReader(path))) {
            List<String[]> rows = reader.readAll();
            if (rows.isEmpty()) return data;
            String[] headers = rows.get(0);
            int sharpCol = -1;
            for (int i = 0; i < headers.length; i++) {
                if ("#".equals(headers[i].trim())) {
                    sharpCol = i;
                    break;
                }
            }
            if (sharpCol >= 0) {
                int lastVal = 0;
                for (int r = 1; r < rows.size(); r++) {
                    String[] row = rows.get(r);
                    if (sharpCol < row.length) {
                        String s = row[sharpCol].trim();
                        if (s.matches("\\d{1,2}")) {
                            try {
                                lastVal = Integer.parseInt(s);
                            } catch (NumberFormatException ignored) {}
                        }
                    }
                }
                if (lastVal > 0) data.put("Strips_Count", lastVal);
            }
        } catch (Exception e) {
            // log
        }
        return data;
    }
}
