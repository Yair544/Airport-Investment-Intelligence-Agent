import type { AnalysisResult, ResultBase } from "../types";
import { ComparisonTable } from "./results/ComparisonTable";
import { LongHaulPanel } from "./results/LongHaulPanel";
import { RankingTable } from "./results/RankingTable";
import { ResultFrame } from "./results/ResultFrame";
import { UnmetDemandPanel } from "./results/UnmetDemandPanel";

/** Renders one validated tool result with the view for its analysis type. */
export function ResultsTable({ result }: { result: AnalysisResult }) {
  switch (result.analysis_type) {
    case "ranking":
      return <RankingTable result={result} />;
    case "comparison":
      return <ComparisonTable result={result} />;
    case "long_haul_share":
      return <LongHaulPanel result={result} />;
    case "unmet_demand":
      return <UnmetDemandPanel result={result} />;
    default: {
      // A result type added on the server before this UI knows it: keep its frame and limitations.
      const unknown = result as unknown as ResultBase;
      return (
        <ResultFrame result={unknown}>
          <p className="muted">This type of result cannot be displayed by this version of the interface.</p>
        </ResultFrame>
      );
    }
  }
}
