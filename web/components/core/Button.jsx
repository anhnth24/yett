import React from 'react';

/** Nút hành động yett. Accent chỉ dành cho primary — tối đa 1 primary/màn hình. */
export function Button({ variant = 'primary', size = 'md', disabled = false, onClick, children, style }) {
  const [hover, setHover] = React.useState(false);
  const variants = {
    primary: { background: 'var(--acc)', color: '#fff', border: '1px solid transparent' },
    secondary: { background: 'var(--card)', color: 'var(--sub)', border: '1px solid var(--line)' },
    danger: { background: 'var(--danger)', color: '#fff', border: '1px solid transparent' },
    ghost: { background: 'transparent', color: 'var(--sub)', border: '1px solid transparent' },
  };
  const v = variants[variant] || variants.primary;
  const hoverFx = variant === 'secondary' || variant === 'ghost'
    ? { background: 'var(--wash)' }
    : { filter: 'brightness(1.1)' };
  return (
    <button
      onClick={disabled ? undefined : onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        ...v,
        ...(hover && !disabled ? hoverFx : null),
        padding: size === 'sm' ? '5px 12px' : '8px 18px',
        borderRadius: 'var(--r-control)',
        fontFamily: 'var(--font-ui)',
        fontSize: size === 'sm' ? '12px' : '12.5px',
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'background 150ms ease-out, filter 150ms ease-out',
        ...style,
      }}
    >{children}</button>
  );
}
