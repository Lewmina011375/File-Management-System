package com.northsail.fpcheck.service;

import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * PDF text extraction (mirrors Python read_pdf_all, read_pdf_pages).
 */
public final class PdfService {

    public static String readPdfAll(Path path) {
        StringBuilder sb = new StringBuilder();
        try (PDDocument doc = Loader.loadPDF(path.toFile())) {
            PDFTextStripper stripper = new PDFTextStripper();
            int pages = doc.getNumberOfPages();
            for (int i = 1; i <= pages; i++) {
                stripper.setStartPage(i);
                stripper.setEndPage(i);
                sb.append(stripper.getText(doc)).append(" ");
            }
        } catch (Exception e) {
            // log
        }
        return sb.toString();
    }

    public static List<String> readPdfPages(Path path) {
        List<String> list = new ArrayList<>();
        try (PDDocument doc = Loader.loadPDF(path.toFile())) {
            PDFTextStripper stripper = new PDFTextStripper();
            int pages = doc.getNumberOfPages();
            for (int i = 1; i <= pages; i++) {
                stripper.setStartPage(i);
                stripper.setEndPage(i);
                list.add(stripper.getText(doc));
            }
        } catch (Exception e) {
            // log
        }
        return list;
    }
}
