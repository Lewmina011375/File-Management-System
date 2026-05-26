package com.northsail.fpcheck.service;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Shared regex and extraction helpers (mirrors Python get_oe, get_dpi, get_tier_*, etc.).
 */
public final class ParseHelper {

    private static final String OE_PREFIXES = "OSP|OUS|OUK|OHK|OFR|ONZ|ONL|OAU|OCA|ODE|OIT|OCR|ODK|OSE";
    private static final Pattern OE_PATTERN = Pattern.compile(
            "\\b((" + OE_PREFIXES + ")\\d{4,10}-\\d{3})\\b", Pattern.CASE_INSENSITIVE);

    public static String getOe(String text) {
        if (text == null || text.isEmpty()) return null;
        Matcher m = OE_PATTERN.matcher(text);
        return m.find() ? m.group(1) : null;
    }

    public static Double getDpi(String text, String patternStr) {
        if (text == null) return null;
        Pattern p = Pattern.compile(patternStr);
        Matcher m = p.matcher(text);
        if (!m.find()) return null;
        String val = m.group(1).replace(",", "");
        try {
            return Double.parseDouble(val);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    public static String getTierBefore(String text) {
        if (text == null) return null;
        Matcher m = Pattern.compile("(\\d{3})\\s*(RAW|ENDURANCE EDGE|ENDURANCE|OCEAN)", Pattern.CASE_INSENSITIVE).matcher(text);
        return m.find() ? m.group(1) : null;
    }

    public static String getTierAfter(String text) {
        if (text == null) return null;
        Matcher m = Pattern.compile("(RAW|ENDURANCE EDGE|ENDURANCE|OCEAN)\\s*(\\d{3})", Pattern.CASE_INSENSITIVE).matcher(text);
        return m.find() ? m.group(2) : null;
    }

    public static String getTierBeforeTaping(String text) {
        if (text == null) return null;
        Matcher m = Pattern.compile("(\\d{3})\\s*(STD|ENDURANCE\\s*EDGE|ENDURANCE|OCEAN|RAW)", Pattern.CASE_INSENSITIVE).matcher(text);
        return m.find() ? m.group(1) : null;
    }

    public static boolean isEnduranceEdge(String text) {
        return text != null && Pattern.compile("endurance\\s*edge", Pattern.CASE_INSENSITIVE).matcher(text).find();
    }

    /** Extract measurements from text file Msm line: Luff, Leech, Foot, LP; Head from Head line. */
    public static double[] getMeasurementsFromTxt(String content) {
        double[] out = new double[5]; // Head, Luff, Leech, Foot, LP - use 0 as missing
        if (content == null) return out;
        String[] lines = content.split("\\n");
        String msmLine = "";
        String headLine = "";
        for (String l : lines) {
            if (l.matches("\\s*Msm.*")) msmLine = l;
            if (l.matches("\\s*Head.*")) headLine = l;
        }
        java.util.regex.Pattern num = java.util.regex.Pattern.compile("[0-9]+\\.[0-9]+");
        Matcher msm = num.matcher(msmLine);
        int i = 0;
        while (msm.find() && i < 4) {
            try {
                out[i + 1] = Double.parseDouble(msm.group()); // Luff=1, Leech=2, Foot=3, LP=4
            } catch (NumberFormatException ignored) {}
            i++;
        }
        Matcher head = num.matcher(headLine);
        if (head.find()) {
            try {
                out[0] = Double.parseDouble(head.group());
            } catch (NumberFormatException ignored) {}
        }
        return out;
    }

    /** Coerce object to Double safely (for comparison). */
    public static Double toDouble(Object v) {
        if (v == null) return null;
        if (v instanceof Number) return ((Number) v).doubleValue();
        String s = v.toString().trim().replace(",", ".");
        if (s.isEmpty() || "-".equals(s)) return null;
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
