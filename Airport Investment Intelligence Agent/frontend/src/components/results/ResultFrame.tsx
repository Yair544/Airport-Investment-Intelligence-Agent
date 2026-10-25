import { useId, type ReactNode } from "react";
import { formatDate, formatParameterValue, humanize, NO_DATA } from "../../lib/format";
import { sourceLabel } from "../../lib/labels";
import type { ResultBase } from "../../types";
import { SourceList } from "../Sources";
import { Limitations, PeriodChips } from "./common";

interface ResultFrameProps {
  result: ResultBase;
  children: ReactNode;
  /** Values that drove the classification, shown inside "Calculation details". */
  inputs?: ReactNode;
}

/** Shared frame of every result: title, completeness, periods, limitations and calculation details. */
export function ResultFrame({ result, children, inputs }: ResultFrameProps) {
  const headingId = useId();
  const defaults = result.defaults_applied.map((key) =>
    key in result.parameters ? `${humanize(key)} = ${formatParameterValue(result.parameters[key] ?? null)}` : humanize(key),
  );
  return (
    <section className="result" aria-labelledby={headingId} dir="ltr">
      <header className="result__header">
        <h3 id={headingId} className="result__title">
          {result.title}
        </h3>
        {result.completeness === "partial" && <span className="badge badge--warn">Partial result</span>}
      </header>
      <PeriodChips periods={result.periods} />
      {defaults.length > 0 && <p className="result__defaults">Defaults applied: {defaults.join(" · ")}</p>}
      <div className="result__body">{children}</div>
      <Limitations items={result.limitations} title="Limitations of this result" />
      <CalculationDetails result={result}>{inputs}</CalculationDetails>
    </section>
  );
}

function CalculationDetails({ result, children }: { result: ResultBase; children?: ReactNode }) {
  const parameters = Object.entries(result.parameters);
  const defaults = new Set(result.defaults_applied);
  const units = Object.entries(result.units);
  return (
    <details className="calc">
      <summary>Calculation details</summary>
      <div className="calc__body">
        <dl className="facts facts--inline">
          <div>
            <dt>Rules version</dt>
            <dd>
              <code>{result.rules_version}</code>
            </dd>
          </div>
          <div>
            <dt>Reference version</dt>
            <dd>{result.reference_version ? <code>{result.reference_version}</code> : NO_DATA}</dd>
          </div>
          <div>
            <dt>Data version</dt>
            <dd>{result.data_version_id ? <code>{result.data_version_id}</code> : NO_DATA}</dd>
          </div>
        </dl>

        <h4 className="calc__heading">Parameters used</h4>
        {parameters.length === 0 ? (
          <p className="muted">No parameters were recorded.</p>
        ) : (
          <div className="table-wrap">
            <table className="data data--compact">
              <thead>
                <tr>
                  <th scope="col">Parameter</th>
                  <th scope="col">Value</th>
                </tr>
              </thead>
              <tbody>
                {parameters.map(([key, value]) => (
                  <tr key={key}>
                    <th scope="row" title={key}>
                      {humanize(key)}
                    </th>
                    <td>
                      {formatParameterValue(value)}
                      {defaults.has(key) && <span className="badge badge--neutral badge--inline">default</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {children}

        {result.periods.length > 0 && (
          <>
            <h4 className="calc__heading">Periods</h4>
            <div className="table-wrap">
              <table className="data data--compact">
                <thead>
                  <tr>
                    <th scope="col">Period</th>
                    <th scope="col">Type</th>
                    <th scope="col">From</th>
                    <th scope="col">To</th>
                    <th scope="col">Source</th>
                    <th scope="col">Role</th>
                  </tr>
                </thead>
                <tbody>
                  {result.periods.map((period, index) => (
                    <tr key={`${period.label}-${index}`}>
                      <th scope="row">{period.label}</th>
                      <td>
                        {period.nature === "forecast" ? "Forecast" : "Historical"} · {humanize(period.kind)}
                      </td>
                      <td>{formatDate(period.start)}</td>
                      <td>{formatDate(period.end)}</td>
                      <td>{sourceLabel(period.source_id)}</td>
                      <td>{period.role ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        {units.length > 0 && (
          <>
            <h4 className="calc__heading">Units</h4>
            <dl className="facts facts--inline">
              {units.map(([key, unit]) => (
                <div key={key}>
                  <dt>{humanize(key)}</dt>
                  <dd>{unit}</dd>
                </div>
              ))}
            </dl>
          </>
        )}

        {result.sources.length > 0 && (
          <>
            <h4 className="calc__heading">Sources used by this result</h4>
            <SourceList sources={result.sources} />
          </>
        )}
      </div>
    </details>
  );
}
