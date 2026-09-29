import { STATUS } from '../data/mock.js';

export default function Pill({ status, small }) {
  const s = STATUS[status];
  return <span style={{ display:"inline-flex", alignItems:"center", gap:7, background:s.bg, color:s.fg,
    fontWeight:600, fontSize:small?14:15.5, padding:small?"4px 10px":"6px 12px", borderRadius:999 }}>
    <span style={{ width:7,height:7,borderRadius:999,background:s.fg }} />{s.label}
  </span>;
}
