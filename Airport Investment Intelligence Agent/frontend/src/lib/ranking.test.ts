import { describe, expect, it } from "vitest";
import { rankingResult } from "../dev/mockData";
import type { RankingRow } from "../types";
import { groupConsecutive, groupLabels } from "./ranking";

describe("groupConsecutive", () => {
  it("keeps the server's group and row order", () => {
    const groups = groupConsecutive(rankingResult.data.rows);
    expect(groups.map((group) => group.group)).toEqual([
      "strong_candidate",
      "watch",
      "external_constraint",
      "information_needed",
      "low_pressure",
    ]);
    expect(groups.flatMap((group) => group.rows.map((row) => row.rank))).toEqual([1, 2, 3, 4, 5, 6]);
    expect(groups[0]?.label).toBe("Strong candidate");
  });

  it("never re-sorts, even when a group appears twice", () => {
    const [a, b, c] = rankingResult.data.rows as [RankingRow, RankingRow, RankingRow];
    const rows = [{ ...a, rank: 1 }, { ...c, rank: 2 }, { ...b, rank: 3, group: a.group }];
    const groups = groupConsecutive(rows);
    expect(groups.map((group) => group.rows.map((row) => row.rank))).toEqual([[1], [2], [3]]);
  });

  it("prefers the server's group labels", () => {
    const [first] = rankingResult.data.rows as [RankingRow];
    const labels = groupLabels([{ ...first, group_label: "Server wording" }]);
    expect(labels.strong_candidate).toBe("Server wording");
    expect(labels.low_pressure).toBe("Low pressure");
  });
});
