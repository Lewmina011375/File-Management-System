package com.northsail.fpcheck.service;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/**
 * Parse text file (mirrors Python parse_txt_file).
 */
public final class TxtParser {

    private static final int TIER_ENDURANCE_EDGE = 360;

    public static Map<String, Object> parse(Path filepath) {
        Map<String, Object> data = new HashMap<>();
        try {
            String content = Files.readString(filepath);
            String fileName = filepath.getFileName().toString();

            String oe = ParseHelper.getOe(content);
            if (oe == null) oe = ParseHelper.getOe(fileName);
            if (oe != null) data.put("OE Number", oe);

            String tierStr = ParseHelper.getTierAfter(content);
            if (tierStr == null) tierStr = ParseHelper.getTierBefore(content);
            if (tierStr != null) {
                try {
                    data.put("Tier", Integer.parseInt(tierStr));
                } catch (NumberFormatException ignored) {}
            }
            if (ParseHelper.isEnduranceEdge(content)) {
                data.put("Tier", TIER_ENDURANCE_EDGE);
            }

            double[] msm = ParseHelper.getMeasurementsFromTxt(content);
            if (msm[0] != 0) data.put("Head", msm[0]);
            if (msm[1] != 0) data.put("Luff", msm[1]);
            if (msm[2] != 0) data.put("Leech", msm[2]);
            if (msm[3] != 0) data.put("Foot", msm[3]);
            if (msm[4] != 0) data.put("LP", msm[4]);

        } catch (Exception e) {
            // log
        }
        return data;
    }
}
