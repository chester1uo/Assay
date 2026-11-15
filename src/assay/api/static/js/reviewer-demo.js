const notice = document.createElement('div');
notice.setAttribute('role', 'status');
Object.assign(notice.style, {position:'fixed', right:'16px', bottom:'16px', zIndex:'10000',
  maxWidth:'360px', background:'#fff', color:'#182235', border:'1px solid #a4b2c4',
  borderRadius:'8px', padding:'10px 14px', boxShadow:'0 2px 12px #0002', fontSize:'13px'});
notice.append('Reviewer demo · Private temporary workspace (7 days). ');
const exit = document.createElement('a');
exit.href = '/demo/exit';
exit.textContent = 'Exit demo';
notice.append(exit);
document.body.append(notice);
