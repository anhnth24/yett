import React from 'react';

/** Chip mono cho tên tool, spec cron, tên secret — dữ liệu máy, nền wash. */
export function Tag({ children, style }) {
  return (
    <span style={{
      fontFamily: 'var(--font-mono)',
      fontSize: '10.5px',
      padding: '2px 8px',
      borderRadius: '5px',
      background: 'var(--wash)',
      color: 'var(--sub)',
      ...style,
    }}>{children}</span>
  );
}
