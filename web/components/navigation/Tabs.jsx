import React from 'react';

/** Segmented control (bố cục dashboard, filter). items: [{value, label}]. Active = card trắng nổi trên wash. */
export function Tabs({ items = [], value, onChange }) {
  return (
    <div style={{ display: 'inline-flex', padding: 3, borderRadius: 9, background: 'var(--wash)', gap: 2, fontFamily: 'var(--font-ui)' }}>
      {items.map((it) => {
        const item = typeof it === 'string' ? { value: it, label: it } : it;
        const on = item.value === value;
        return (
          <div
            key={item.value}
            onClick={() => onChange && onChange(item.value)}
            style={{
              padding: '5px 14px', borderRadius: 7, fontSize: '12px', fontWeight: 600, cursor: 'pointer',
              background: on ? 'var(--card)' : 'transparent',
              color: on ? 'var(--ink)' : 'var(--mut)',
              boxShadow: on ? 'var(--shadow-raised)' : 'none',
              transition: 'background 150ms ease-out',
            }}
          >{item.label}</div>
        );
      })}
    </div>
  );
}
