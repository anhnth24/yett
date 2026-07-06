import React from 'react';

/** Card chuẩn yett: nền trắng, viền --line, shadow 1 mức. feature=true → radius 14. */
export function Card({ title, meta, feature = false, children, style }) {
  return (
    <div style={{
      background: 'var(--card)',
      border: '1px solid var(--line)',
      borderRadius: feature ? 'var(--r-feature)' : 'var(--r-card)',
      boxShadow: 'var(--shadow-card)',
      padding: '18px 20px',
      fontFamily: 'var(--font-ui)',
      ...style,
    }}>
      {title ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
          <div style={{ fontSize: '13.5px', fontWeight: 700, color: 'var(--ink)', flex: 1 }}>{title}</div>
          {meta ? <div style={{ fontSize: '11px', color: 'var(--faint)' }}>{meta}</div> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
