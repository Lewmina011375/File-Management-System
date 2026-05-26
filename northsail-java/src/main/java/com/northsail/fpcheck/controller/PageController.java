package com.northsail.fpcheck.controller;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

@Controller
public class PageController {

    @GetMapping("/")
    public String index() {
        return "index";
    }

    @GetMapping("/battens-pocket")
    public String battensPocket() {
        return "battens_pocket";
    }

    @GetMapping("/other-criteria")
    public String otherCriteria() {
        return "other_criteria";
    }
}
