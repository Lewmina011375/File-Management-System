package com.northsail.fpcheck.service;

import org.apache.poi.ss.usermodel.*;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;

import java.io.ByteArrayOutputStream;
import java.util.List;
import java.util.Map;

public final class ExcelExport {

    public static byte[] toExcel(List<Map<String, Object>> results) {
        try (Workbook wb = new XSSFWorkbook(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            Sheet sheet = wb.createSheet("Results");
            String[] headers = {"Criteria", "Text_File", "Taping_Report", "Work_Ticket", "Difference", "Status"};
            Row headerRow = sheet.createRow(0);
            for (int i = 0; i < headers.length; i++) {
                Cell c = headerRow.createCell(i);
                c.setCellValue(headers[i]);
            }
            int rowNum = 1;
            for (Map<String, Object> row : results) {
                Row r = sheet.createRow(rowNum++);
                for (int i = 0; i < headers.length; i++) {
                    Object v = row.get(headers[i]);
                    Cell cell = r.createCell(i);
                    if (v != null) cell.setCellValue(v.toString());
                }
            }
            wb.write(out);
            return out.toByteArray();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
