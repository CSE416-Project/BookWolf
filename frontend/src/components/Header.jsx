const NAV = {
  leader: [["find","Find a space"],["requests","My requests"],["messages","Messages"],["forum","Forum"]],
  admin: [["approvals","Approvals"],["rooms","Rooms"],["forum","Forum"],["messages","Messages"]],
  venue: [["rooms","Rooms"],["messages","Messages"]],
};
const ROLE_LABEL = { leader:"Club leader", admin:"Administrator", venue:"Venue host" };
const AVATAR = { leader:"RC", admin:"SA", venue:"VH" };
const DEFAULT_PAGE = { leader:"find", admin:"approvals", venue:"rooms" };

export default function Header({ role, setRole, page, setPage }) {
  const nav = NAV[role];
  return (
    <header style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"0 24px",
      height:64, background:"var(--card)", borderBottom:"1px solid var(--line)", position:"sticky", top:0, zIndex:20, flexWrap:"wrap", gap:8 }}>
      <div style={{ display:"flex", alignItems:"center", gap:28, flexWrap:"wrap" }}>
        <div style={{ display:"flex", alignItems:"center", gap:9 }}>
          <div style={{ width:28, height:28, borderRadius:8, background:"var(--ink)", display:"grid", placeItems:"center" }}>
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:2.5 }}>
              <span style={{ width:6,height:6,borderRadius:1.5,background:"var(--green)" }} />
              <span style={{ width:6,height:6,borderRadius:1.5,background:"#fff" }} />
              <span style={{ width:6,height:6,borderRadius:1.5,background:"#fff" }} />
              <span style={{ width:6,height:6,borderRadius:1.5,background:"var(--amber)" }} />
            </div>
          </div>
          <span style={{ fontWeight:800, fontSize:19 }}>CampusReserve</span>
        </div>
        <nav style={{ display:"flex", gap:4 }}>
          {nav.map(([id,label]) => (
            <button key={id} onClick={() => setPage(id)} style={{
              fontSize:15, fontWeight:500, padding:"8px 13px", borderRadius:7,
              color: page===id ? "var(--ink)" : "var(--ink-faint)",
              background: page===id ? "var(--line-soft)" : "transparent" }}>{label}</button>
          ))}
        </nav>
      </div>
      <div style={{ display:"flex", alignItems:"center", gap:14 }}>
        <div style={{ display:"flex", background:"var(--line-soft)", borderRadius:9, padding:3 }}>
          {["leader","admin","venue"].map(r => (
            <button key={r} onClick={() => { setRole(r); setPage(DEFAULT_PAGE[r]); }} style={{
              fontSize:13.5, fontWeight:600, padding:"7px 12px", borderRadius:7,
              background: role===r ? "var(--card)" : "transparent",
              color: role===r ? "var(--ink)" : "var(--ink-faint)",
              boxShadow: role===r ? "var(--shadow)" : "none" }}>
              {ROLE_LABEL[r]}
            </button>
          ))}
        </div>
        <div style={{ width:34, height:34, borderRadius:999, background:"var(--blue-wash)", color:"var(--blue)",
          display:"grid", placeItems:"center", fontWeight:700, fontSize:14 }}>
          {AVATAR[role]}
        </div>
      </div>
    </header>
  );
}
