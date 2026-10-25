import type { RankingGroup, RankingRow } from "../types";
import { RANKING_GROUP_LABELS } from "./labels";

export interface RowGroup {
  group: RankingGroup;
  label: string;
  rows: RankingRow[];
}

/**
 * Splits rows into consecutive runs of the same group. The server already orders groups and rows;
 * this never sorts, so the displayed order is exactly the received order.
 */
export function groupConsecutive(rows: readonly RankingRow[]): RowGroup[] {
  const groups: RowGroup[] = [];
  for (const row of rows) {
    const last = groups[groups.length - 1];
    if (last && last.group === row.group) {
      last.rows.push(row);
    } else {
      groups.push({ group: row.group, label: row.group_label || RANKING_GROUP_LABELS[row.group], rows: [row] });
    }
  }
  return groups;
}

/** Labels for every group, preferring the server's wording when a row carries it. */
export function groupLabels(rows: readonly RankingRow[]): Record<RankingGroup, string> {
  const labels = { ...RANKING_GROUP_LABELS };
  for (const row of rows) {
    if (row.group_label) labels[row.group] = row.group_label;
  }
  return labels;
}
