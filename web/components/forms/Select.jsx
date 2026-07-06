import React from 'react';

/** Select yett: cùng grammar với Input. options: [{value, label}] hoặc string[]. */
export function Select({ label, options = [], value, onChange, style }) {
  const [focus, setFocus] = React.useState(false);
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 5, fontFamily: 'var(--font-ui)', ...style }}>
      {label ? <span style={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--sub)' }}>{label}</span> : null}
      <select
        value={value}
        onChange={onChange}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{
          padding: '8px 11px',
          borderRadius: 'var(--r-control)',
          border: '1px solid ' + (focus ? 'var(--acc)' : 'var(--line)'),
          outline: 'none',
          background: 'var(--card)',
          color: 'var(--body)',
          fontFamily: 'var(--font-ui)',
          fontSize: '13px',
          cursor: 'pointer',
        }}
      >
        {options.map((o) => {
          const opt = typeof o === 'string' ? { value: o, label: o } : o;
          return <option key={opt.value} value={opt.value}>{opt.label}</option>;
        })}
      </select>
    </label>
  );
}
