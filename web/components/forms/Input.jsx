import React from 'react';

/** Input yett: label trên, viền --line, focus ring accent. mono=true cho giá trị máy. */
export function Input({ label, placeholder, value, onChange, onKeyDown, mono = false, error, type = 'text', style }) {
  const [focus, setFocus] = React.useState(false);
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 5, fontFamily: 'var(--font-ui)', ...style }}>
      {label ? <span style={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--sub)' }}>{label}</span> : null}
      <input
        type={type}
        placeholder={placeholder}
        value={value}
        onChange={onChange}
        onKeyDown={onKeyDown}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{
          padding: '8px 11px',
          borderRadius: 'var(--r-control)',
          border: '1px solid ' + (error ? 'var(--danger)' : focus ? 'var(--acc)' : 'var(--line)'),
          outline: 'none',
          background: 'var(--card)',
          color: 'var(--body)',
          fontFamily: mono ? 'var(--font-mono)' : 'var(--font-ui)',
          fontSize: mono ? '12px' : '13px',
          transition: 'border-color 150ms ease-out',
        }}
      />
      {error ? <span style={{ fontSize: '11.5px', color: 'var(--danger)' }}>{error}</span> : null}
    </label>
  );
}
