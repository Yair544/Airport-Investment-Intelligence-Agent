export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg className="logo" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="7" fill="currentColor" />
      <g fill="#ffffff" transform="translate(4 4)">
        <rect x="11" y="2" width="2" height="20" rx="1" />
        <path d="M2 13.5 12 8l10 5.5v2L12 12 2 15.5z" />
        <path d="M8 20.5 12 18.5l4 2v1.5l-4-1.2-4 1.2z" />
      </g>
    </svg>
  );
}
