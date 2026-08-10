import { describe, expect, it } from "vitest";

import {
  dayLabel,
  duration,
  isoDaysAgo,
  istDay,
  istDayWindow,
  languageName,
  monthLabel,
  number,
  percent,
  phone,
  rupees,
  shiftDay,
  signedPercent,
  titleCase,
} from "./format";

describe("rupees", () => {
  it("formats paise as INR", () => {
    expect(rupees(399900)).toBe("₹3,999.00");
  });

  it("compacts large values into lakhs", () => {
    expect(rupees(15_000_000, { compact: true })).toBe("₹1.50L");
  });

  it("does not compact values under a lakh", () => {
    expect(rupees(9_999_900, { compact: true })).toBe("₹99,999.00");
  });
});

describe("number", () => {
  it("uses Indian digit grouping", () => {
    expect(number(1234567)).toBe("12,34,567");
  });
});

describe("duration", () => {
  it("renders zero as 0s", () => {
    expect(duration(0)).toBe("0s");
  });

  it("renders sub-minute durations as seconds only", () => {
    expect(duration(45)).toBe("45s");
  });

  it("renders whole minutes without a seconds part", () => {
    expect(duration(120)).toBe("2m");
  });

  it("renders minutes and seconds", () => {
    expect(duration(125)).toBe("2m 5s");
  });
});

describe("percent", () => {
  it("formats a fraction as a percentage", () => {
    expect(percent(0.1234)).toBe("12.3%");
  });
});

describe("signedPercent", () => {
  it("prefixes positive values with a plus sign", () => {
    expect(signedPercent(12.4)).toBe("+12.4%");
  });

  it("leaves negative values as-is", () => {
    expect(signedPercent(-3.2)).toBe("-3.2%");
  });

  it("does not sign zero", () => {
    expect(signedPercent(0)).toBe("0.0%");
  });
});

describe("titleCase", () => {
  it("title-cases snake_case", () => {
    expect(titleCase("in_progress")).toBe("In Progress");
  });

  it("title-cases space separated words", () => {
    expect(titleCase("hot lead")).toBe("Hot Lead");
  });
});

describe("phone", () => {
  it("splits a 10-digit Indian number into groups", () => {
    expect(phone("+919876543210")).toBe("+91 98765 43210");
  });

  it("returns non-matching values unchanged", () => {
    expect(phone("+14155551234")).toBe("+14155551234");
  });
});

describe("languageName", () => {
  it("maps known codes to display names", () => {
    expect(languageName("hi")).toBe("Hindi");
  });

  it("falls back to the uppercased code", () => {
    expect(languageName("xx")).toBe("XX");
  });

  it("renders null as an em dash", () => {
    expect(languageName(null)).toBe("—");
  });
});

describe("IST day handling", () => {
  it("rolls a late-UTC instant into the next IST calendar day", () => {
    // 2026-08-09T19:00:00Z is 2026-08-10T00:30 IST — slicing the raw ISO
    // string would give the UTC day (08-09), which is wrong once UTC passes
    // 18:30 (IST midnight). This pins the IST-aware behavior.
    expect(istDay("2026-08-09T19:00:00Z")).toBe("2026-08-10");
  });

  it("computes the UTC window covering one IST day", () => {
    const { from, to } = istDayWindow("2026-08-09");
    expect(from).toBe("2026-08-08T18:30:00.000Z");
    expect(to).toBe("2026-08-09T18:30:00.000Z");
  });

  it("shifts a day forward and backward across a month boundary", () => {
    expect(shiftDay("2026-08-31", 1)).toBe("2026-09-01");
    expect(shiftDay("2026-09-01", -1)).toBe("2026-08-31");
  });

  it("labels a day with weekday, day and month", () => {
    expect(dayLabel("2026-08-10")).toBe("Monday, 10 August");
  });

  it("computes a day N days back", () => {
    const today = new Date();
    const expected = new Date(today);
    expected.setDate(expected.getDate() - 7);
    expect(isoDaysAgo(7)).toBe(
      expected.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }),
    );
  });
});

describe("monthLabel", () => {
  it("renders a YYYY-MM month as a full label", () => {
    expect(monthLabel("2026-08")).toBe("August 2026");
  });

  it("returns input with no month segment unchanged", () => {
    expect(monthLabel("malformed")).toBe("malformed");
  });
});
