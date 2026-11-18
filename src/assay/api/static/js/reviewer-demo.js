import { getLang, onLang } from './i18n.js';

const notice = document.createElement('div');
notice.setAttribute('role', 'status');
Object.assign(notice.style, {position:'fixed', right:'16px', bottom:'16px', zIndex:'10000',
  maxWidth:'360px', background:'#fff', color:'#182235', border:'1px solid #a4b2c4',
  borderRadius:'8px', padding:'10px 14px', boxShadow:'0 2px 12px #0002', fontSize:'13px'});
const message = document.createTextNode('');
notice.append(message);
const exit = document.createElement('a');
exit.href = '/demo/exit';
notice.append(exit);
document.body.append(notice);
function localize() {
  const zh = getLang() === 'zh';
  message.textContent = zh ? '审稿演示 · 私有临时工作区（15 天）。' : 'Reviewer demo · Private temporary workspace (15 days). ';
  exit.textContent = zh ? '退出演示' : 'Exit demo';
}
localize();
onLang(localize);
