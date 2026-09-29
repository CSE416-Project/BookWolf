import { useState } from 'react';
import { ROOMS, THREADS, FORUM_POSTS, getDays } from './data/mock.js';
import Header from './components/Header.jsx';
import FindSpace from './pages/FindSpace.jsx';
import RoomDetail from './pages/RoomDetail.jsx';
import BookingDrawer from './pages/BookingDrawer.jsx';
import MyRequests from './pages/MyRequests.jsx';
import Approvals from './pages/Approvals.jsx';
import RoomsAdmin from './pages/RoomsAdmin.jsx';
import VenueRooms from './pages/VenueRooms.jsx';
import Messages from './pages/Messages.jsx';
import Forum from './pages/Forum.jsx';

const D = getDays(3);
const SELF_ORG = { leader:"Robotics Club", admin:"Student Affairs", venue:"Venue Host" };

export default function App() {
  const [role,setRole]=useState("leader");
  const [page,setPage]=useState("find");
  const [viewingRoom,setViewingRoom]=useState(null);
  const [openRoom,setOpenRoom]=useState(null);

  const [requests,setRequests]=useState([
    { id:1, room:ROOMS[1], org:"Dance Collective", date:D[1].label, time:"12:00", status:"pending" },
    { id:2, room:ROOMS[0], org:"Robotics Club", date:D[2].label, time:"18:00", status:"approved" },
  ]);
  const submitRequest = (room,picks) => {
    setRequests(r => [...r, ...picks.map((p,i) => ({ id:Date.now()+i, room, org:"Robotics Club", date:p.label, time:p.time, status:"pending" }))]);
  };
  const decide = (id,status,reason) => {
    setRequests(r => r.map(req => req.id===id ? { ...req, status, reason } : req));
  };

  const [threads,setThreads]=useState(THREADS);
  const [activeThread,setActiveThread]=useState(null);
  const selfName = SELF_ORG[role];
  const sendMessage = (threadId,text) => {
    setThreads(ts => ts.map(t => t.id===threadId
      ? { ...t, messages:[...t.messages, { from:selfName, text, time:"Just now" }] } : t));
  };
  const startChat = (contact) => {
    const existing = threads.find(t => t.participants.includes(selfName) && t.participants.includes(contact));
    if (existing) { setActiveThread(existing.id); return; }
    const id = Date.now();
    setThreads(ts => [...ts, { id, participants:[selfName,contact], subject:"New conversation", messages:[] }]);
    setActiveThread(id);
  };

  const [posts,setPosts]=useState(FORUM_POSTS);
  const addPost = (title,body) => setPosts(ps => [{ id:ps.length+1, org:selfName, title, body, replies:[] }, ...ps]);
  const addReply = (postId,text) => setPosts(ps => ps.map(p => p.id===postId ? { ...p, replies:[...p.replies, { org:selfName, text }] } : p));

  const changeRole = (r, defaultPage) => { setRole(r); setPage(defaultPage); setViewingRoom(null); setActiveThread(null); };

  return (
    <div>
      <Header role={role} setRole={r=>changeRole(r, r==="leader"?"find":r==="admin"?"approvals":"rooms")} page={page} setPage={p=>{ setPage(p); setViewingRoom(null); }} />

      {viewingRoom ? (
        <RoomDetail room={viewingRoom} onBack={()=>setViewingRoom(null)} onBook={setOpenRoom} />
      ) : (
        <>
          {role==="leader" && page==="find" && <FindSpace onView={setViewingRoom} onBook={setOpenRoom} />}
          {role==="leader" && page==="requests" && <MyRequests requests={requests.filter(r=>r.org==="Robotics Club")} />}
          {role==="admin" && page==="approvals" && <Approvals requests={requests} onDecide={decide} />}
          {role==="admin" && page==="rooms" && <RoomsAdmin />}
          {role==="venue" && page==="rooms" && <VenueRooms requests={requests} />}
          {(role==="leader" || role==="admin") && page==="forum" && <Forum posts={posts} onPost={addPost} onReply={addReply} />}
          {page==="messages" && <Messages threads={threads} onSend={sendMessage} onStartChat={startChat}
            activeId={activeThread} setActiveId={setActiveThread} selfName={selfName} />}
        </>
      )}

      {openRoom && <BookingDrawer room={openRoom} onClose={()=>setOpenRoom(null)} onSubmit={submitRequest} />}
    </div>
  );
}
