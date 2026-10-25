#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <fabgl.h>
#include <esp_heap_caps.h>

// VGA32 Thin Client 8 - ESP32 core 2.0.17 / FabGL 1.0.9 / LILYGO TTGO VGA32 v1.4
// The ESP32 is a "head": a 16-color paletted framebuffer, a local mouse cursor and
// PS/2 keyboard + mouse. Everything else (windows, apps, fonts) lives on the host,
// which streams damage rectangles and receives input events over TCP 7777.
// The board announces itself every 2s on UDP 7778 so hosts can find it.
// WiFi and the host key live in secrets.h (not in git): copy secrets.example.h to secrets.h.
#include "secrets.h"
const char *DEVICE_NAME = "vga32";

// 0 = 640x480 (154KB framebuffer), 1 = 640x400 (128KB), 2 = 512x384 (98KB).
// Use a smaller profile if the serial log shows WiFi failing for lack of heap.
#define TC_RES_PROFILE 1
#if TC_RES_PROFILE == 1
constexpr int W=640,H=400;
#define TC_VIDEO_MODE VGA_640x400_70Hz
#elif TC_RES_PROFILE == 2
constexpr int W=512,H=384;
#define TC_VIDEO_MODE VGA_512x384_60Hz
#else
constexpr int W=640,H=480;
#define TC_VIDEO_MODE VGA_640x480_60Hz
#endif

constexpr uint8_t PROTO_VERSION=1;
constexpr uint16_t TCP_PORT=7777, BEACON_PORT=7778;
constexpr uint16_t MAX_BODY=4200;
constexpr int CUR_MAX=32;
constexpr size_t WIFI_RESERVE=32*1024;  // heap kept away from FabGL for the WiFi driver

fabgl::VGA16Controller VGA;
fabgl::PS2Controller PS2;
WiFiServer tcp(TCP_PORT);
WiFiClient peer;
WiFiUDP udp;

// Host -> device opcodes. Frame: op u8, body length u16 LE, body. All ints LE.
enum : uint8_t {
  OP_HELLO=0x01,   // key bytes
  OP_PALETTE=0x02, // n x (index, r, g, b)
  OP_FILL=0x03,    // x y w h i16, color
  OP_BLIT4=0x04,   // x y w h u16, rows of ceil(w/2) bytes, even pixel = high nibble
  OP_BLITRLE=0x05, // x y w h u16, runs [color<<4|n] n=1..15, n=0 -> next byte+16
  OP_COPY=0x06,    // sx sy w h dx dy u16 (overlap safe)
  OP_TEXT=0x07,    // x y i16, fg, bg (255 = transparent), font, chars
  OP_LINE=0x08,    // x1 y1 x2 y2 i16, color
  OP_RECT=0x09,    // x y w h i16, color (outline)
  OP_CURSOR=0x0A,  // 0 hidden | 1 arrow | 2 w h hx hy + w*h pixels (255 = transparent)
  OP_WARP=0x0B,    // x y u16
  OP_SYNC=0x0C,    // token u32, answered once everything before it is on screen
  OP_PING=0x0D,
  OP_CLEAR=0x0E,   // color
};
// Device -> host events.
enum : uint8_t {
  EV_HELLO=0x81,   // proto, w u16, h u16, bpp, kbd, mouse, nfonts, (fw, fh)*, name
  EV_KEY=0x82,     // vk u16, down, ascii, mods
  EV_MOUSE=0x83,   // x u16, y u16, buttons (1 L, 2 R, 4 M), wheel i8
  EV_SYNC=0x84,    // token u32
  EV_PONG=0x85,
  EV_ERROR=0x86,   // code, op
};
enum : uint8_t {ERR_AUTH=1,ERR_BAD_OP=2,ERR_BAD_ARGS=3,ERR_TOO_BIG=4};

const fabgl::FontInfo *FONTS[]={&fabgl::FONT_6x8,&fabgl::FONT_8x8,&fabgl::FONT_8x14,&fabgl::FONT_8x16,&fabgl::FONT_10x20};
constexpr int FONT_COUNT=sizeof(FONTS)/sizeof(FONTS[0]);

// ================= FRAMEBUFFER =================
// Direct 4bpp writes into FabGL's scanlines; Canvas (and its primitive queue) is never used.
static inline uint8_t *row(int y){return fabgl::VGA16Controller::sgetScanline(y);}
static inline void putPx(int x,int y,uint8_t c){
  uint8_t *p=row(y)+(x>>1);*p=(x&1)?((*p&0xF0)|c):((*p&0x0F)|(c<<4));
}
static inline uint8_t getPx(int x,int y){uint8_t v=row(y)[x>>1];return (x&1)?(v&15):(v>>4);}
static inline void plot(int x,int y,uint8_t c){if(x>=0&&y>=0&&x<W&&y<H)putPx(x,y,c);}
void hspan(int x0,int x1,int y,uint8_t c){
  if(y<0||y>=H)return;
  if(x0<0)x0=0; if(x1>=W)x1=W-1; if(x0>x1)return;
  c&=15;
  if(x0&1){putPx(x0,y,c);x0++;}
  if(!(x1&1)&&x1>=x0){putPx(x1,y,c);x1--;}
  if(x1>x0)memset(row(y)+(x0>>1),(c<<4)|c,(x1-x0+1)>>1);
}
void fillRect(int x,int y,int w,int h,uint8_t c){for(int yy=max(0,y);yy<min(H,y+h);yy++)hspan(x,x+w-1,yy,c);}
void outlineRect(int x,int y,int w,int h,uint8_t c){
  if(w<=0||h<=0)return;
  hspan(x,x+w-1,y,c);hspan(x,x+w-1,y+h-1,c);
  for(int yy=y+1;yy<y+h-1;yy++){plot(x,yy,c);plot(x+w-1,yy,c);}
}
void drawLine(int x0,int y0,int x1,int y1,uint8_t c){
  int dx=abs(x1-x0),sx=x0<x1?1:-1,dy=-abs(y1-y0),sy=y0<y1?1:-1,err=dx+dy;
  for(;;){plot(x0,y0,c);if(x0==x1&&y0==y1)break;int e2=2*err;
    if(e2>=dy){err+=dy;x0+=sx;} if(e2<=dx){err+=dx;y0+=sy;}}
}
void drawText(int x,int y,const uint8_t *s,int n,uint8_t fg,uint8_t bg,int fi){
  const fabgl::FontInfo *f=FONTS[fi];
  int fw=f->width,fh=f->height,rb=(fw+7)/8;
  for(int i=0;i<n;i++,x+=fw){
    if(x>=W)break; if(x+fw<=0)continue;
    const uint8_t *g=f->data+(size_t)s[i]*rb*fh;
    for(int r=0;r<fh;r++)for(int col=0;col<fw;col++){
      bool on=g[r*rb+(col>>3)]&(0x80>>(col&7));
      if(on)plot(x+col,y+r,fg); else if(bg!=255)plot(x+col,y+r,bg);
    }
  }
}
void drawText(int x,int y,const char *s,uint8_t fg,uint8_t bg=255,int fi=3){drawText(x,y,(const uint8_t*)s,strlen(s),fg,bg,fi);}
void copyRect(int sx,int sy,int w,int h,int dx,int dy){
  static uint8_t tmp[W];
  bool up=dy>sy;
  for(int i=0;i<h;i++){int r=up?h-1-i:i;
    for(int c=0;c<w;c++)tmp[c]=getPx(sx+c,sy+r);
    for(int c=0;c<w;c++)putPx(dx+c,dy+r,tmp[c]);
  }
}
void defaultPalette(){for(int i=0;i<16;i++)VGA.setPaletteItem(i,fabgl::RGB888((fabgl::Color)i));}

// ================= LOCAL CURSOR =================
// Drawn by the board itself so pointer motion never waits on the network.
// Draw ops that touch the cursor hide it first; it is redrawn at the end of each loop.
const char *ARROW[]={
  "X           ","XX          ","X.X         ","X..X        ","X...X       ",
  "X....X      ","X.....X     ","X......X    ","X.......X   ","X........X  ",
  "X.........X ","X......XXXXX","X...X..X    ","X..XX..X    ","X.X  X..X   ",
  "XX   X..X   ","X     X..X  ","      X..X  ","       XX   "};
struct {
  int x=W/2,y=H/2,w=0,h=0,hx=0,hy=0;
  bool enabled=true,drawn=false;
  int sx,sy,sw,sh;
  uint8_t pix[CUR_MAX*CUR_MAX],save[CUR_MAX*CUR_MAX];
} cur;
void cursorArrow(){
  cur.w=12;cur.h=19;cur.hx=0;cur.hy=0;
  for(int y=0;y<cur.h;y++)for(int x=0;x<cur.w;x++){char ch=ARROW[y][x];
    cur.pix[y*cur.w+x]=ch=='X'?0:ch=='.'?15:255;}
}
void cursorHide(){
  if(!cur.drawn)return;
  for(int y=0;y<cur.sh;y++)for(int x=0;x<cur.sw;x++)putPx(cur.sx+x,cur.sy+y,cur.save[y*cur.sw+x]);
  cur.drawn=false;
}
void cursorShow(){
  if(cur.drawn||!cur.enabled||!cur.w)return;
  int ox=cur.x-cur.hx,oy=cur.y-cur.hy;
  int x0=max(0,ox),y0=max(0,oy),x1=min(W,ox+cur.w),y1=min(H,oy+cur.h);
  if(x0>=x1||y0>=y1)return;
  cur.sx=x0;cur.sy=y0;cur.sw=x1-x0;cur.sh=y1-y0;
  for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++){
    cur.save[(y-y0)*cur.sw+(x-x0)]=getPx(x,y);
    uint8_t c=cur.pix[(y-oy)*cur.w+(x-ox)];
    if(c!=255)putPx(x,y,c&15);
  }
  cur.drawn=true;
}
// Hide the cursor if a draw into (x,y,w,h) would overlap it.
void guard(int x,int y,int w,int h){
  if(cur.drawn&&x<cur.sx+cur.sw&&x+w>cur.sx&&y<cur.sy+cur.sh&&y+h>cur.sy)cursorHide();
}

// ================= INPUT =================
int mouseX=W/2,mouseY=H/2,mouseWheel=0;
uint8_t mouseButtons=0;
bool mouseDirty=false;
uint32_t mouseSentAt=0;
char lastKey[24]="(none)";
bool statusDirty=true;

// ================= NETWORK =================
uint8_t hdr[3],body[MAX_BODY];
uint8_t hdrHave=0,op=0;
uint16_t bodyWant=0,bodyHave=0;
bool authed=false;
uint32_t lastRx=0,lastWifiAttempt=0,lastBeacon=0;
uint32_t rxBytes=0,opsDone=0;
uint8_t out[1024];
size_t outLen=0;

void flushOut(){if(outLen&&peer.connected())peer.write(out,outLen);outLen=0;}
void emit(uint8_t ev,const uint8_t *b,uint16_t n){
  if(!peer.connected())return;
  if(outLen+3+n>sizeof(out))flushOut();
  out[outLen++]=ev;out[outLen++]=n&255;out[outLen++]=n>>8;
  memcpy(out+outLen,b,n);outLen+=n;
}
void emitError(uint8_t code){uint8_t b[2]={code,op};emit(EV_ERROR,b,2);}
static inline uint16_t u16(const uint8_t *p){return p[0]|(p[1]<<8);}
static inline int16_t i16(const uint8_t *p){return (int16_t)u16(p);}

void drawStatus();
void closePeer(){
  if(peer)peer.stop();
  authed=false;hdrHave=0;bodyHave=0;bodyWant=0;outLen=0;
  cursorHide();defaultPalette();cursorArrow();cur.enabled=true;statusDirty=true;
  Serial.println("[NET] host disconnected");
}
void sendHello(){
  uint8_t b[64];int n=0;
  b[n++]=PROTO_VERSION;b[n++]=W&255;b[n++]=W>>8;b[n++]=H&255;b[n++]=H>>8;b[n++]=4;
  b[n++]=PS2.keyboard()&&PS2.keyboard()->isKeyboardAvailable();
  b[n++]=PS2.mouse()&&PS2.mouse()->isMouseAvailable();
  b[n++]=FONT_COUNT;
  for(int i=0;i<FONT_COUNT;i++){b[n++]=FONTS[i]->width;b[n++]=FONTS[i]->height;}
  size_t nl=strlen(DEVICE_NAME);if(nl>20)nl=20;memcpy(b+n,DEVICE_NAME,nl);n+=nl;
  emit(EV_HELLO,b,n);
  uint8_t m[6]={(uint8_t)(mouseX&255),(uint8_t)(mouseX>>8),(uint8_t)(mouseY&255),(uint8_t)(mouseY>>8),mouseButtons,0};
  emit(EV_MOUSE,m,6);
}
bool inScreen(int x,int y,int w,int h){return w>0&&h>0&&x>=0&&y>=0&&x+w<=W&&y+h<=H;}

void execute(){
  const uint8_t *p=body;uint16_t n=bodyWant;
  opsDone++;
  if(!authed){
    size_t kl=strlen(HOST_KEY);
    if(op==OP_HELLO&&n==kl&&memcmp(p,HOST_KEY,kl)==0){
      authed=true;Serial.println("[NET] host authenticated");sendHello();
    }else{emitError(ERR_AUTH);flushOut();closePeer();}
    return;
  }
  switch(op){
    case OP_HELLO:sendHello();break;
    case OP_PALETTE:
      if(n%4){emitError(ERR_BAD_ARGS);break;}
      for(int i=0;i<n;i+=4)VGA.setPaletteItem(p[i]&15,fabgl::RGB888(p[i+1],p[i+2],p[i+3]));
      break;
    case OP_FILL:case OP_RECT:{
      if(n!=9){emitError(ERR_BAD_ARGS);break;}
      int x=i16(p),y=i16(p+2),w=i16(p+4),h=i16(p+6);
      guard(x,y,w,h);
      if(op==OP_FILL)fillRect(x,y,w,h,p[8]&15);else outlineRect(x,y,w,h,p[8]&15);
      break;}
    case OP_BLIT4:{
      if(n<8){emitError(ERR_BAD_ARGS);break;}
      int x=u16(p),y=u16(p+2),w=u16(p+4),h=u16(p+6),rb=(w+1)/2;
      if(!inScreen(x,y,w,h)||n!=8+rb*h){emitError(ERR_BAD_ARGS);break;}
      guard(x,y,w,h);
      for(int r=0;r<h;r++){const uint8_t *s=p+8+r*rb;
        if(!(x&1)){memcpy(row(y+r)+(x>>1),s,w>>1);if(w&1)putPx(x+w-1,y+r,s[w>>1]>>4);}
        else for(int i=0;i<w;i++)putPx(x+i,y+r,(i&1)?(s[i>>1]&15):(s[i>>1]>>4));
      }
      break;}
    case OP_BLITRLE:{
      if(n<8){emitError(ERR_BAD_ARGS);break;}
      int x=u16(p),y=u16(p+2),w=u16(p+4),h=u16(p+6);
      if(!inScreen(x,y,w,h)){emitError(ERR_BAD_ARGS);break;}
      guard(x,y,w,h);
      int cx=0,cy=0;
      for(int i=8;i<n&&cy<h;){
        uint8_t b=p[i++],c=b>>4;int len=b&15;
        if(!len){if(i>=n)break;len=16+p[i++];}
        while(len>0&&cy<h){int take=min(len,w-cx);
          hspan(x+cx,x+cx+take-1,y+cy,c);cx+=take;len-=take;
          if(cx==w){cx=0;cy++;}}
      }
      break;}
    case OP_COPY:{
      if(n!=12){emitError(ERR_BAD_ARGS);break;}
      int sx=u16(p),sy=u16(p+2),w=u16(p+4),h=u16(p+6),dx=u16(p+8),dy=u16(p+10);
      if(!inScreen(sx,sy,w,h)||!inScreen(dx,dy,w,h)){emitError(ERR_BAD_ARGS);break;}
      guard(sx,sy,w,h);guard(dx,dy,w,h);copyRect(sx,sy,w,h,dx,dy);
      break;}
    case OP_TEXT:{
      if(n<7||p[6]>=FONT_COUNT){emitError(ERR_BAD_ARGS);break;}
      int x=i16(p),y=i16(p+2),cnt=n-7;
      guard(x,y,cnt*FONTS[p[6]]->width,FONTS[p[6]]->height);
      drawText(x,y,p+7,cnt,p[4]&15,p[5],p[6]);
      break;}
    case OP_LINE:{
      if(n!=9){emitError(ERR_BAD_ARGS);break;}
      int x1=i16(p),y1=i16(p+2),x2=i16(p+4),y2=i16(p+6);
      guard(min(x1,x2),min(y1,y2),abs(x2-x1)+1,abs(y2-y1)+1);
      drawLine(x1,y1,x2,y2,p[8]&15);
      break;}
    case OP_CURSOR:
      if(n<1){emitError(ERR_BAD_ARGS);break;}
      cursorHide();
      if(p[0]==0)cur.enabled=false;
      else if(p[0]==1){cursorArrow();cur.enabled=true;}
      else if(p[0]==2&&n>=5){int w=p[1],h=p[2];
        if(w<1||h<1||w>CUR_MAX||h>CUR_MAX||n!=5+w*h){emitError(ERR_BAD_ARGS);break;}
        cur.w=w;cur.h=h;cur.hx=min((int)p[3],w-1);cur.hy=min((int)p[4],h-1);
        memcpy(cur.pix,p+5,w*h);cur.enabled=true;
      }else emitError(ERR_BAD_ARGS);
      break;
    case OP_WARP:
      if(n!=4){emitError(ERR_BAD_ARGS);break;}
      cursorHide();mouseX=constrain((int)u16(p),0,W-1);mouseY=constrain((int)u16(p+2),0,H-1);
      cur.x=mouseX;cur.y=mouseY;mouseDirty=true;
      break;
    case OP_SYNC:
      if(n!=4){emitError(ERR_BAD_ARGS);break;}
      emit(EV_SYNC,p,4);break;
    case OP_PING:emit(EV_PONG,nullptr,0);break;
    case OP_CLEAR:
      if(n!=1){emitError(ERR_BAD_ARGS);break;}
      cursorHide();fillRect(0,0,W,H,p[0]&15);break;
    default:emitError(ERR_BAD_OP);
  }
}

void netAccept(){
  WiFiClient incoming=tcp.available();
  if(!incoming)return;
  // Latest connection wins, so a restarted host never waits on a stale socket.
  if(peer)closePeer();
  peer=incoming;peer.setNoDelay(true);lastRx=millis();
  Serial.printf("[NET] host %s connected\n",peer.remoteIP().toString().c_str());
}
void netRead(){
  if(!peer)return;
  if(!peer.connected()){closePeer();return;}
  uint32_t now=millis();
  if(now-lastRx>(authed?20000UL:5000UL)){Serial.println("[NET] host timeout");closePeer();return;}
  // Bounded work per loop keeps PS/2 input and the cursor responsive during big blits.
  int budget=48*1024;uint32_t t0=now;
  while(budget>0&&millis()-t0<25){
    int avail=peer.available();if(avail<=0)break;
    int got;
    if(hdrHave<3){
      got=peer.read(hdr+hdrHave,3-hdrHave);if(got<=0)break;hdrHave+=got;
      if(hdrHave==3){op=hdr[0];bodyWant=hdr[1]|(hdr[2]<<8);bodyHave=0;
        if(bodyWant>MAX_BODY){emitError(ERR_TOO_BIG);flushOut();closePeer();return;}
        if(!bodyWant){execute();hdrHave=0;if(!peer)return;}}
    }else{
      got=peer.read(body+bodyHave,min(avail,(int)(bodyWant-bodyHave)));if(got<=0)break;
      bodyHave+=got;
      if(bodyHave==bodyWant){execute();hdrHave=0;bodyHave=0;if(!peer)return;}
    }
    budget-=got;rxBytes+=got;lastRx=millis();
  }
}

void pollKeyboard(){
  auto kb=PS2.keyboard();
  if(!kb||!kb->isKeyboardAvailable())return;
  fabgl::VirtualKeyItem k;
  while(kb->virtualKeyAvailable()&&kb->getNextVirtualKey(&k,0)){
    uint8_t mods=k.CTRL|(k.LALT<<1)|(k.RALT<<2)|(k.SHIFT<<3)|(k.GUI<<4)|(k.CAPSLOCK<<5)|(k.NUMLOCK<<6)|(k.SCROLLLOCK<<7);
    if(authed){uint8_t b[5]={(uint8_t)(k.vk&255),(uint8_t)(k.vk>>8),k.down,k.ASCII,mods};emit(EV_KEY,b,5);}
    else if(k.down){
      if(k.ASCII>=32&&k.ASCII<127)snprintf(lastKey,sizeof(lastKey),"'%c' vk=%d",k.ASCII,(int)k.vk);
      else snprintf(lastKey,sizeof(lastKey),"vk=%d",(int)k.vk);
      statusDirty=true;
    }
  }
}
void pollMouse(){
  auto m=PS2.mouse();
  if(!m||!m->isMouseAvailable())return;
  fabgl::MouseDelta d;
  while(m->deltaAvailable()&&m->getNextDelta(&d,0)){
    int dx=d.deltaX,dy=-d.deltaY;
    // Mild acceleration: precise when slow, crosses 640px quickly when fast.
    if(abs(dx)>6)dx=dx*3/2; if(abs(dy)>6)dy=dy*3/2;
    int nx=constrain(mouseX+dx,0,W-1),ny=constrain(mouseY+dy,0,H-1);
    uint8_t nb=d.buttons.left|(d.buttons.right<<1)|(d.buttons.middle<<2);
    if(nx!=mouseX||ny!=mouseY||nb!=mouseButtons||d.deltaZ){
      // Local scribble when no host is attached proves the mouse works end to end.
      if(!authed&&(nb&1)){cursorHide();drawLine(mouseX,mouseY,nx,ny,14);}
      mouseX=nx;mouseY=ny;mouseWheel+=d.deltaZ;
      if(nb!=mouseButtons){mouseButtons=nb;mouseSentAt=0;}  // buttons go out immediately
      mouseDirty=true;
    }
  }
  if(cur.x!=mouseX||cur.y!=mouseY){cursorHide();cur.x=mouseX;cur.y=mouseY;}
}
void sendMouse(){
  if(!mouseDirty||!authed)return;
  if(millis()-mouseSentAt<8&&!mouseWheel)return;  // coalesce motion to ~125Hz
  uint8_t b[6]={(uint8_t)(mouseX&255),(uint8_t)(mouseX>>8),(uint8_t)(mouseY&255),(uint8_t)(mouseY>>8),
    mouseButtons,(uint8_t)(int8_t)constrain(mouseWheel,-127,127)};
  emit(EV_MOUSE,b,6);mouseWheel=0;mouseDirty=false;mouseSentAt=millis();
}

// ================= STATUS SCREEN (no host attached) =================
int lastWifiState=-1;
void drawStatus(){
  cursorHide();
  fillRect(0,0,W,H,4);
  fillRect(0,0,W,28,1);
  drawText(10,6,"VGA32 THIN CLIENT 8",15,255,3);
  char line[96];
  snprintf(line,sizeof(line),"%dx%d  16 colors  proto %d",W,H,PROTO_VERSION);drawText(W-8-strlen(line)*8,6,line,15,255,3);
  int y=60;
  if(WiFi.status()==WL_CONNECTED){
    snprintf(line,sizeof(line),"WiFi  %s  (%d dBm)",WIFI_SSID,(int)WiFi.RSSI());drawText(30,y,line,10);y+=24;
    snprintf(line,sizeof(line),"Host  %s:%d",WiFi.localIP().toString().c_str(),TCP_PORT);drawText(30,y,line,14);y+=24;
  }else{
    snprintf(line,sizeof(line),"WiFi  connecting to %s ...",WIFI_SSID);drawText(30,y,line,11);y+=48;
  }
  auto kb=PS2.keyboard();auto ms=PS2.mouse();
  snprintf(line,sizeof(line),"Keyboard  %s",kb&&kb->isKeyboardAvailable()?"OK":"not detected (plug in, reset)");drawText(30,y,line,15);y+=24;
  snprintf(line,sizeof(line),"Mouse     %s",ms&&ms->isMouseAvailable()?"OK (hold left button to draw)":"not detected (plug in, reset)");drawText(30,y,line,15);y+=24;
  snprintf(line,sizeof(line),"Last key  %s",lastKey);drawText(30,y,line,15);y+=24;
  snprintf(line,sizeof(line),"Heap      %u free, %u largest",ESP.getFreeHeap(),heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));drawText(30,y,line,7);y+=48;
  drawText(30,y,"Waiting for host.  On the PC run:",15);y+=24;
  drawText(46,y,"python -m vga32 desktop",11);
  statusDirty=false;
}

void updateWiFi(){
  int st=WiFi.status();
  if(st!=WL_CONNECTED&&millis()-lastWifiAttempt>12000){
    lastWifiAttempt=millis();WiFi.disconnect(false);WiFi.begin(WIFI_SSID,WIFI_PASS);
    Serial.printf("[WIFI] retry %s (status %d)\n",WIFI_SSID,st);
  }
  if(st!=lastWifiState){lastWifiState=st;statusDirty=true;
    if(st==WL_CONNECTED)Serial.printf("[WIFI] %s  tcp %d\n",WiFi.localIP().toString().c_str(),TCP_PORT);}
  if(st==WL_CONNECTED&&millis()-lastBeacon>2000){
    lastBeacon=millis();
    char msg[96];int n=snprintf(msg,sizeof(msg),"VGA32TC/%d name=%s w=%d h=%d port=%d busy=%d",
      PROTO_VERSION,DEVICE_NAME,W,H,TCP_PORT,authed?1:0);
    udp.beginPacket(IPAddress(255,255,255,255),BEACON_PORT);udp.write((const uint8_t*)msg,n);udp.endPacket();
  }
}

void setup(){
  Serial.begin(115200);delay(200);
  Serial.println("VGA32 THIN CLIENT 8");
  PS2.begin(fabgl::PS2Preset::KeyboardPort0_MousePort1,fabgl::KbdMode::CreateVirtualKeysQueue);
  // FabGL takes heap until the largest free block drops under 40KB, which leaves WiFi
  // with too little (WiFi.begin -> ESP_ERR_NO_MEM, "set config failed"). Starting WiFi
  // first instead fragments the heap and FabGL silently allocates fewer scanlines.
  // So hold a block back for WiFi while the framebuffer is allocated.
  void *wifiReserve=heap_caps_malloc(WIFI_RESERVE,MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL);
  VGA.begin();VGA.setResolution(TC_VIDEO_MODE);
  heap_caps_free(wifiReserve);
  Serial.printf("[VGA] %dx%d 4bpp  heap=%u largest=%u\n",W,H,ESP.getFreeHeap(),heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
  if(!wifiReserve||VGA.getViewPortHeight()<H){
    // Drawing past the allocated lines would write through garbage pointers.
    Serial.printf("[VGA] only %d of %d lines allocated - lower TC_RES_PROFILE or WIFI_RESERVE\n",VGA.getViewPortHeight(),H);
    for(;;)delay(1000);
  }
  defaultPalette();cursorArrow();
  drawStatus();
  // Reason 2/15/202 = bad password, 201 = SSID not found (or 5GHz only).
  WiFi.onEvent([](WiFiEvent_t,WiFiEventInfo_t info){
    Serial.printf("[WIFI] disconnected, reason %d  heap=%u\n",info.wifi_sta_disconnected.reason,ESP.getFreeHeap());
  },ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  WiFi.persistent(false);WiFi.mode(WIFI_STA);WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID,WIFI_PASS);lastWifiAttempt=millis();
  tcp.begin();tcp.setNoDelay(true);
  Serial.printf("[READY] heap=%u largest=%u\n",ESP.getFreeHeap(),heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
}
void loop(){
  updateWiFi();
  netAccept();
  pollKeyboard();pollMouse();
  netRead();
  sendMouse();
  flushOut();
  if(!authed&&statusDirty)drawStatus();
  cursorShow();
  static uint32_t logAt=0;
  if(millis()-logAt>15000){logAt=millis();
    Serial.printf("[RUN] host=%d rx=%u ops=%u heap=%u\n",authed,rxBytes,opsDone,ESP.getFreeHeap());}
  delay(1);
}
