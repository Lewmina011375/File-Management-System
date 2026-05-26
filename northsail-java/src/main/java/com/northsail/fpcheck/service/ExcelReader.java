package com.northsail.fpcheck.service;

import org.apache.poi.ss.usermodel.*;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

public final class ExcelReader {

    public static Map<String, Object> readWorkTicket(Path path) {
        Map<String, Object> data = new HashMap<>();
        try (var is = Files.newInputStream(path); Workbook wb = new XSSFWorkbook(is)) {
            Sheet sheet = wb.getSheetAt(0);
            int sharpCol = -1;
            Row headerRow = sheet.getRow(0);
            if (headerRow != null) {
                for (int i = 0; i < headerRow.getLastCellNum(); i++) {
                    Cell c = headerRow.getCell(i);
                    if (c != null && "#".equals(getCellString(c))) {
                        sharpCol = i;
                        break;
                    }
                }
            }
            if (sharpCol >= 0) {
                int lastVal = 0;
                for (int r = 1; r <= sheet.getLastRowNum(); r++) {
                    Row row = sheet.getRow(r);
                    if (row == null) continue;
                    Cell cell = row.getCell(sharpCol);
                    String s = getCellString(cell);
                    if (s != null && s.matches("\\d{1,2}")) {
                        try {
                            lastVal = Integer.parseInt(s);
                        } catch (NumberFormatException ignored) {}
                    }
                }
                if (lastVal > 0) data.put("Strips_Count", lastVal);
            }
        } catch (Exception e) {
            // log
        }
        return data;
    }

    private static String getCellString(Cell c) {
        if (c == null) return null;
        return switch (c.getCellType()) {
            case STRING -> c.getStringCellValue();
            case NUMERIC -> String.valueOf((long) c.getNumericCellValue());
            case BOOLEAN -> String.valueOf(c.getBooleanCellValue());
            default -> null;
        };
    }
}
