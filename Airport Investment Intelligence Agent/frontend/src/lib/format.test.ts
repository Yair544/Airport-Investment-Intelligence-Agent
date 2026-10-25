import { describe, expect, it } from "vitest";
import {
  formatComparison,
  formatDate,
  formatDateTime,
  formatElapsed,
  formatInteger,
  formatMinutes,
  formatNumber,
  formatParameterValue,
  formatPercent,
  formatSample,
  formatShortDateTime,
  formatSigned,
  formatSignedPercent,
  formatWithUnit,
  humanize,
  monthLabel,
  NO_DATA,
} from "./format";
import { safeHttpUrl } from "./url";

// Intl may use narrow no-break spaces (for example before "PM"); compare with plain spaces.
const plain = (text: string) => text.replace(/\s/g, " ");

describe("number formatting", () => {
  it("shows missing values as No data, never as zero", () => {
    for (const value of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(formatNumber(value)).toBe(NO_DATA);
      expect(formatPercent(value)).toBe(NO_DATA);
      expect(formatSigned(value)).toBe(NO_DATA);
      expect(formatMinutes(value)).toBe(NO_DATA);
    }
    expect(formatInteger(0)).toBe("0");
  });

  it("formats integers, signed changes and percent values", () => {
    expect(formatInteger(37_812_345.4)).toBe("37,812,345");
    expect(formatSigned(4_512_345)).toBe("+4,512,345");
    expect(formatSigned(-5)).toBe("-5");
    expect(formatSigned(0)).toBe("0");
    expect(formatPercent(84.234)).toBe("84.2%");
    expect(formatPercent(2.4449, 2)).toBe("2.44%");
    expect(formatSignedPercent(4.1)).toBe("+4.1%");
    expect(formatMinutes(6)).toBe("6.0 min");
    expect(formatSample(1234)).toBe("n = 1,234");
    expect(formatSample(null)).toBe("");
  });

  it("formats values in the unit the server reports", () => {
    expect(formatWithUnit(84.2, "%")).toBe("84.2%");
    expect(formatWithUnit(2.1, "% per year", 2)).toBe("2.10%");
    expect(formatWithUnit(6.4, "minutes")).toBe("6.4 min");
    expect(formatWithUnit(5, "flights")).toBe("5.0 flights");
    expect(formatWithUnit(null, "%")).toBe(NO_DATA);
  });

  it("compares a value with its reference and adds decimals only when needed to tell them apart", () => {
    expect(formatComparison(84.2, 83.1, "%")).toBe("84.2% vs 83.1%");
    expect(formatComparison(83.42, 83.4, "%")).toBe("83.42% vs 83.40%");
    expect(formatComparison(2.084, 2.081, "% per year", 2)).toBe("2.084% vs 2.081%");
    expect(formatComparison(6, 4.1, "min")).toBe("6.0 min vs 4.1 min");
    expect(formatComparison(null, 83.1, "%")).toBe("No data vs 83.1%");
    expect(formatComparison(83.1, 83.1, "%")).toBe("83.1% vs 83.1%");
  });
});

describe("date formatting", () => {
  it("formats timestamps in a given time zone", () => {
    expect(plain(formatDateTime("2026-10-23T13:45:00Z", "UTC"))).toBe("Oct 23, 2026, 1:45 PM");
    expect(formatDateTime(null)).toBe(NO_DATA);
    expect(formatDateTime("not a date")).toBe("not a date");
  });

  it("keeps calendar dates on the same day regardless of the viewer's time zone", () => {
    expect(formatDate("2026-10-01")).toBe("Oct 1, 2026");
    expect(formatDate(null)).toBe(NO_DATA);
  });

  it("uses a compact form for lists", () => {
    const now = new Date("2026-10-23T18:00:00Z");
    const options = { now, timeZone: "UTC" };
    expect(plain(formatShortDateTime("2026-10-23T09:05:00Z", options))).toBe("9:05 AM");
    expect(formatShortDateTime("2026-10-21T09:05:00Z", options)).toBe("Oct 21");
    expect(formatShortDateTime("2025-10-21T09:05:00Z", options)).toBe("Oct 21, 2025");
  });

  it("formats elapsed time and month labels", () => {
    expect(formatElapsed(7)).toBe("0:07");
    expect(formatElapsed(125.9)).toBe("2:05");
    expect(monthLabel(1, 2025)).toBe("Jan 2025");
    expect(monthLabel(12)).toBe("Dec");
    expect(monthLabel(13)).toBe("13");
  });
});

describe("labels and parameters", () => {
  it("humanizes keys and shows parameters verbatim", () => {
    expect(humanize("threshold_miles")).toBe("Threshold miles");
    expect(formatParameterValue(2025)).toBe("2025");
    expect(formatParameterValue(3452.5)).toBe("3452.5");
    expect(formatParameterValue(["CT", "MA"])).toBe("CT, MA");
    expect(formatParameterValue(true)).toBe("Yes");
    expect(formatParameterValue(null)).toBe("None");
  });
});

describe("safeHttpUrl", () => {
  it("allows only absolute http and https URLs", () => {
    expect(safeHttpUrl("https://taf.faa.gov/")).toBe("https://taf.faa.gov/");
    expect(safeHttpUrl("  http://example.com/a b ")).toBe("http://example.com/a%20b");
    for (const unsafe of [
      "javascript:alert(1)",
      "JAVASCRIPT:alert(1)",
      "data:text/html,<b>x</b>",
      "mailto:someone@example.com",
      "/relative/path",
      "//example.com/protocol-relative",
      "https://user:secret@example.com/",
      "",
      null,
      undefined,
    ]) {
      expect(safeHttpUrl(unsafe)).toBeNull();
    }
  });
});
