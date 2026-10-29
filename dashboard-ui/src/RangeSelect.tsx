import { RANGE_LABEL } from "./format";

export function RangeSelect({ value, onChange }: { value: string; onChange: (range: string) => void }) {
  return (
    <select className="field" aria-label="Time range" value={value} onChange={(e) => onChange(e.target.value)}>
      {Object.entries(RANGE_LABEL).map(([key, label]) => (
        <option key={key} value={key}>
          {label}
        </option>
      ))}
    </select>
  );
}
