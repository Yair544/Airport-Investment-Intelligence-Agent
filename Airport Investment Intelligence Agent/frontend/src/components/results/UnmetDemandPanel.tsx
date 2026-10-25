import {
  formatComparison,
  formatInteger,
  formatPercent,
  formatSignedPercent,
  isNumber,
  monthLabel,
  NO_DATA,
} from "../../lib/format";
import { DELAY_CAUSE_LABELS, DELAY_CAUSES, signalStatusLabel } from "../../lib/labels";
import type { MonthlyDelayCauses, SignalResult, UnmetDemandData, UnmetDemandResult } from "../../types";
import { AirportName, ConstraintList, EvidenceList, StatusBadge } from "./common";
import { ResultFrame } from "./ResultFrame";

const CLASSIFICATION_TONE: Record<UnmetDemandData["classification"], string> = {
  constrained_demand_signs: "warn",
  weak_demand_signs: "neutral",
  undetermined: "unknown",
};

function SignalLine({ signal }: { signal: SignalResult }) {
  return (
    <li className="signal-line">
      <StatusBadge status={signal.status} />{" "}
      <span>
        {signal.signal} {signal.name}: {formatComparison(signal.value, signal.reference, signal.unit)}
      </span>
      {signal.note && <span className="cell-sub">{signal.note}</span>}
    </li>
  );
}

function causeShare(month: MonthlyDelayCauses, cause: (typeof DELAY_CAUSES)[number]): string {
  return formatPercent(month.causes.find((entry) => entry.cause === cause)?.share);
}

function signalSummary(signal: SignalResult): string {
  const sample = isNumber(signal.sample_size) ? ` · n = ${formatInteger(signal.sample_size)}` : "";
  return `${signalStatusLabel(signal.status)} · ${formatComparison(signal.value, signal.reference, signal.unit)}${sample}`;
}

/** Inputs of the three-valued classification, for "Calculation details". */
function ClassificationInputs({ data }: { data: UnmetDemandData }) {
  const rows: Array<[string, string]> = [
    ["Passenger growth", formatSignedPercent(data.passenger_growth)],
    ["Seat growth", formatSignedPercent(data.seat_growth)],
    [`S2 ${data.s2.name}`, signalSummary(data.s2)],
    [`S3 ${data.s3.name}`, signalSummary(data.s3)],
    ["Documented constraint", data.constraints.length > 0 ? "Yes" : "None on file"],
  ];
  return (
    <>
      <h4 className="calc__heading">Values behind the classification</h4>
      <dl className="facts">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}

export function UnmetDemandPanel({ result }: { result: UnmetDemandResult }) {
  const data = result.data;
  const year = data.analysis_year;
  const previous = data.comparison_year;

  return (
    <ResultFrame result={result} inputs={<ClassificationInputs data={data} />}>
      <div className="classification">
        <span className={`badge badge--lg badge--${CLASSIFICATION_TONE[data.classification]}`}>
          {data.classification_label}
        </span>
        <AirportName airport={data.airport} />
      </div>
      <p className="rule-line">
        <span className="rule-line__label">Rule applied:</span> <span dir="auto">{data.rule}</span>
      </p>

      <h4 className="section-title">Conditions</h4>
      <ul className="conditions">
        {data.conditions.map((condition) => (
          <li key={condition.condition_id}>
            <StatusBadge status={condition.status} kind="condition" /> <span>{condition.description}</span>
            {condition.detail && <span className="cell-sub">{condition.detail}</span>}
          </li>
        ))}
      </ul>

      <div className="split">
        <div>
          <h4 className="section-title">
            Passengers vs seats, CY{previous} → CY{year}
          </h4>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Measure</th>
                  <th scope="col" className="num">
                    CY{previous}
                  </th>
                  <th scope="col" className="num">
                    CY{year}
                  </th>
                  <th scope="col" className="num">
                    Growth
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Passengers</th>
                  <td className="num">{formatInteger(data.passengers_previous)}</td>
                  <td className="num">{formatInteger(data.passengers)}</td>
                  <td className="num">{formatSignedPercent(data.passenger_growth)}</td>
                </tr>
                <tr>
                  <th scope="row">Seats</th>
                  <td className="num">{formatInteger(data.seats_previous)}</td>
                  <td className="num">{formatInteger(data.seats)}</td>
                  <td className="num">{formatSignedPercent(data.seat_growth)}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h4 className="section-title">Pressure signals vs national reference, CY{year}</h4>
          <ul className="signal-lines">
            <SignalLine signal={data.s2} />
            <SignalLine signal={data.s3} />
          </ul>
        </div>
      </div>

      <h4 className="section-title">Documented constraints</h4>
      {data.constraints.length === 0 ? (
        <p className="muted">None on file.</p>
      ) : (
        <ConstraintList constraints={data.constraints} />
      )}

      <h4 className="section-title">Infrastructure evidence</h4>
      {data.evidence.length === 0 ? (
        <p className="muted">No infrastructure evidence on file.</p>
      ) : (
        <EvidenceList evidence={data.evidence} />
      )}

      <h4 className="section-title">Monthly delay causes at arrival, CY{year}</h4>
      {data.monthly_delay_causes.length === 0 ? (
        <p className="muted">No monthly delay-cause data was returned.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption className="visually-hidden">
              Share of attributed arrival delay minutes by cause and month
            </caption>
            <thead>
              <tr>
                <th scope="col">Month</th>
                <th scope="col" className="num">
                  Arrivals
                </th>
                <th scope="col" className="num">
                  Attributed delay
                  <span className="th-sub">minutes</span>
                </th>
                {DELAY_CAUSES.map((cause) => (
                  <th key={cause} scope="col" className="num">
                    {DELAY_CAUSE_LABELS[cause]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.monthly_delay_causes.map((month) => (
                <tr key={`${month.year}-${month.month}`}>
                  <th scope="row">{monthLabel(month.month, month.year)}</th>
                  <td className="num">{formatInteger(month.arrivals)}</td>
                  <td className="num">
                    {isNumber(month.attributed_delay_minutes) ? formatInteger(month.attributed_delay_minutes) : NO_DATA}
                  </td>
                  {DELAY_CAUSES.map((cause) => (
                    <td key={cause} className="num">
                      {causeShare(month, cause)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="footnote">
        Shares are percentages of arrival delay minutes with a reported cause. NAS also covers non-extreme weather
        and air traffic control.
      </p>
    </ResultFrame>
  );
}
