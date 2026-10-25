import {
  formatComparison,
  formatInteger,
  formatMinutes,
  formatPercent,
  formatSample,
  formatSigned,
  formatWithUnit,
  isNumber,
  NO_DATA,
} from "../../lib/format";
import { constraintTypeLabel, SIGNAL_LABELS } from "../../lib/labels";
import { groupConsecutive, groupLabels } from "../../lib/ranking";
import type { NationalReference, RankingGroup, RankingResult, RankingRow, SignalResult } from "../../types";
import { AirportName, ConstraintList, EvidenceList, Notes, StatusBadge } from "./common";
import { ResultFrame } from "./ResultFrame";

type SignalId = "S1" | "S2" | "S3";
const SIGNAL_IDS: readonly SignalId[] = ["S1", "S2", "S3"];
/** S1 compares growth rates that often differ in the second decimal. */
const SIGNAL_DIGITS: Record<SignalId, number> = { S1: 2, S2: 1, S3: 1 };
const COLUMN_COUNT = 11;

/** The server's rule text already starts with its id ("R2: ..."); the id is shown separately. */
function ruleText(row: RankingRow): string {
  const prefix = `${row.rule_id}: `;
  return row.rule.startsWith(prefix) ? row.rule.slice(prefix.length) : row.rule;
}

function signalOf(row: RankingRow, id: SignalId): SignalResult | undefined {
  return row.signals.find((signal) => signal.signal === id);
}

function formatReference(reference: NationalReference | undefined): string {
  if (!reference || !isNumber(reference.value)) return NO_DATA;
  switch (reference.metric) {
    case "taf_cagr":
      return `${formatPercent(reference.value, 2)}/yr`;
    case "t100_load_factor":
      return formatPercent(reference.value, 1);
    case "ontime_taxi_out_excess_median":
      return formatMinutes(reference.value, 1);
    default:
      return String(reference.value);
  }
}

const REFERENCE_LABELS: Record<SignalId, string> = {
  S1: "S1 TAF CAGR",
  S2: "S2 load factor",
  S3: "S3 median taxi-out excess",
};

function NationalReferences({ references }: { references: RankingResult["data"]["national_references"] }) {
  return (
    <p className="refs">
      <span className="refs__title">National references</span>
      {SIGNAL_IDS.map((id) => {
        const reference = references[id];
        const sample = formatSample(reference?.sample_size);
        return (
          <span key={id} className="refs__item">
            {REFERENCE_LABELS[id]}
            {reference?.period_key ? ` (${reference.period_key})` : ""}: <strong>{formatReference(reference)}</strong>
            {sample && <span className="muted"> · {sample}</span>}
          </span>
        );
      })}
    </p>
  );
}

function SignalCell({ signal, id }: { signal: SignalResult | undefined; id: SignalId }) {
  if (!signal) {
    return (
      <td className="signal-cell">
        <StatusBadge status="unknown" /> <span className="signal-cell__values muted">{NO_DATA}</span>
      </td>
    );
  }
  return (
    <td className="signal-cell" title={signal.note ?? undefined}>
      <StatusBadge status={signal.status} />{" "}
      <span className="signal-cell__values">
        {formatComparison(signal.value, signal.reference, signal.unit, SIGNAL_DIGITS[id])}
      </span>
    </td>
  );
}

function Change({
  delta,
  base,
  horizon,
  baseYear,
  horizonYear,
}: {
  delta: number | null;
  base: number | null;
  horizon: number | null;
  baseYear: number;
  horizonYear: number;
}) {
  return (
    <>
      <span className="cell-main">{formatSigned(delta)}</span>
      <span className="cell-sub">
        FY{baseYear}: {formatInteger(base)}
      </span>
      <span className="cell-sub">
        FY{horizonYear}: {formatInteger(horizon)}
      </span>
    </>
  );
}

function RowView({
  row,
  labels,
  baseYear,
  horizonYear,
}: {
  row: RankingRow;
  labels: Record<RankingGroup, string>;
  baseYear: number;
  horizonYear: number;
}) {
  return (
    <tr>
      <td className="num sticky-col sticky-col--rank">{row.rank}</td>
      <th scope="row" className="airport-cell sticky-col sticky-col--airport">
        <AirportName airport={row.airport} />
        {row.notes.length > 0 && <Notes notes={row.notes} />}
      </th>
      <td className="num">
        <Change
          delta={row.delta_enplanements}
          base={row.base_year_enplanements}
          horizon={row.horizon_enplanements}
          baseYear={baseYear}
          horizonYear={horizonYear}
        />
      </td>
      <td className="num">{formatPercent(row.cagr_5, 2)}</td>
      <td className="num">
        <Change
          delta={row.delta_commercial_operations}
          base={row.base_year_commercial_operations}
          horizon={row.horizon_commercial_operations}
          baseYear={baseYear}
          horizonYear={horizonYear}
        />
      </td>
      {SIGNAL_IDS.map((id) => (
        <SignalCell key={id} id={id} signal={signalOf(row, id)} />
      ))}
      <td className="rule-cell">
        {row.rule_id !== "UNKNOWN" && <span className="rule-id">{row.rule_id}</span>} <span>{ruleText(row)}</span>
        {row.possible_groups.length > 0 && (
          <span className="cell-sub">Possible groups: {row.possible_groups.map((group) => labels[group]).join(" / ")}</span>
        )}
      </td>
      <td>
        {row.constraints.length === 0 ? (
          <span className="muted">None</span>
        ) : (
          <span className="badge-stack">
            {row.constraints.map((constraint, index) => (
              <span key={index} className="badge badge--warn" title={constraint.description}>
                {constraintTypeLabel(constraint.constraint_type)}
              </span>
            ))}
          </span>
        )}
      </td>
      <td>
        {row.further_review ? (
          <span
            className="badge badge--review"
            title="No terminal evidence on file: a candidate for further review, not a proven need."
          >
            Further review
          </span>
        ) : row.evidence_status === "available" ? (
          <span className="muted">Evidence on file</span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  );
}

/** Values behind each classification, for the "Calculation details" disclosure. */
function ClassificationInputs({ rows }: { rows: RankingRow[] }) {
  const withRecords = rows.filter((row) => row.constraints.length > 0 || row.evidence.length > 0);
  return (
    <>
      <h4 className="calc__heading">Values behind each classification</h4>
      <div className="table-wrap">
        <table className="data data--compact">
          <thead>
            <tr>
              <th scope="col">Airport</th>
              <th scope="col">Group and rule</th>
              <th scope="col">Signal</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Value
              </th>
              <th scope="col" className="num">
                Reference
              </th>
              <th scope="col" className="num">
                Sample
              </th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const signals = SIGNAL_IDS.map((id) => ({ id, signal: signalOf(row, id) }));
              return signals.map(({ id, signal }, index) => (
                <tr key={`${row.rank}-${id}`} className={index === 0 ? "row-start" : undefined}>
                  {index === 0 && (
                    <>
                      <th scope="row" rowSpan={signals.length}>
                        <AirportName airport={row.airport} />
                      </th>
                      <td rowSpan={signals.length}>
                        {row.group_label}
                        <span className="cell-sub">
                          {row.rule_id !== "UNKNOWN" && `${row.rule_id}: `}
                          {ruleText(row)}
                        </span>
                      </td>
                    </>
                  )}
                  <td>
                    {id} {signal?.name ?? SIGNAL_LABELS[id]}
                  </td>
                  <td>
                    <StatusBadge status={signal?.status ?? "unknown"} />
                  </td>
                  <td className="num">{signal ? formatWithUnit(signal.value, signal.unit, SIGNAL_DIGITS[id]) : NO_DATA}</td>
                  <td className="num">
                    {signal ? formatWithUnit(signal.reference, signal.unit, SIGNAL_DIGITS[id]) : NO_DATA}
                  </td>
                  <td className="num">{signal && isNumber(signal.sample_size) ? formatInteger(signal.sample_size) : "—"}</td>
                  <td>{signal?.note ?? "—"}</td>
                </tr>
              ));
            })}
          </tbody>
        </table>
      </div>
      {withRecords.length > 0 && (
        <>
          <h4 className="calc__heading">Constraints and evidence on file</h4>
          {withRecords.map((row) => (
            <div key={row.rank} className="airport-records">
              <p className="airport-records__title">
                <AirportName airport={row.airport} />
              </p>
              {row.constraints.length > 0 && <ConstraintList constraints={row.constraints} />}
              {row.evidence.length > 0 && <EvidenceList evidence={row.evidence} />}
            </div>
          ))}
        </>
      )}
    </>
  );
}

export function RankingTable({ result }: { result: RankingResult }) {
  const data = result.data;
  const groups = groupConsecutive(data.rows);
  const labels = groupLabels(data.rows);
  const { base_year: baseYear, horizon_year: horizonYear, analysis_year: year } = data;

  return (
    <ResultFrame result={result} inputs={data.rows.length > 0 ? <ClassificationInputs rows={data.rows} /> : null}>
      <p className="result__meta">
        {data.region_label && <>{data.region_label} · </>}
        {data.taf_edition} · TAF forecast FY{baseYear} → FY{horizonYear} (fiscal years) · Load factor and taxi-out
        signals from CY{year} (historical calendar year)
      </p>
      <NationalReferences references={data.national_references} />

      {data.rows.length === 0 ? (
        <p className="empty-note">No airports met the selection criteria.</p>
      ) : (
        <div className="table-wrap">
          <table className="data data--ranking">
            <caption className="visually-hidden">
              Airports grouped by classification, in the order returned by the analysis
            </caption>
            <thead>
              <tr>
                <th scope="col" className="num sticky-col sticky-col--rank">
                  #
                </th>
                <th scope="col" className="sticky-col sticky-col--airport">
                  Airport
                </th>
                <th scope="col" className="num">
                  ΔE enplanements
                  <span className="th-sub">
                    FY{baseYear} → FY{horizonYear}, forecast
                  </span>
                </th>
                <th scope="col" className="num">
                  CAGR
                  <span className="th-sub">%/yr, forecast</span>
                </th>
                <th scope="col" className="num">
                  Δ Commercial operations
                  <span className="th-sub">
                    FY{baseYear} → FY{horizonYear}, forecast
                  </span>
                </th>
                <th scope="col">
                  S1 {SIGNAL_LABELS.S1}
                  <span className="th-sub">airport vs national CAGR</span>
                </th>
                <th scope="col">
                  S2 {SIGNAL_LABELS.S2}
                  <span className="th-sub">CY{year}, historical</span>
                </th>
                <th scope="col">
                  S3 {SIGNAL_LABELS.S3}
                  <span className="th-sub">CY{year}, historical</span>
                </th>
                <th scope="col">Decided by</th>
                <th scope="col">Constraint</th>
                <th scope="col">Review</th>
              </tr>
            </thead>
            {groups.map((group, index) => (
              <tbody key={`${group.group}-${index}`}>
                <tr className="group-row">
                  <th scope="colgroup" colSpan={COLUMN_COUNT}>
                    <span className="group-row__label">
                      {group.label} <span className="group-row__count">({group.rows.length})</span>
                    </span>
                  </th>
                </tr>
                {group.rows.map((row) => (
                  <RowView
                    key={`${row.rank}-${row.airport.airport_id}`}
                    row={row}
                    labels={labels}
                    baseYear={baseYear}
                    horizonYear={horizonYear}
                  />
                ))}
              </tbody>
            ))}
          </table>
        </div>
      )}

      {data.excluded.length > 0 && (
        <details className="note">
          <summary>
            {data.excluded.length === 1 ? "1 airport excluded" : `${data.excluded.length} airports excluded`}
          </summary>
          <ul className="plain-list">
            {data.excluded.map((excluded, index) => (
              <li key={`${excluded.airport.airport_id}-${index}`}>
                <AirportName airport={excluded.airport} />: <span dir="auto">{excluded.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </ResultFrame>
  );
}
