/* Dedicated UB400 HCI relay for rooted Android. TLS requires a client certificate.
 * USB traffic is restricted to interface 0 of VID 0a12 / PID 0001. No system BT API.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/usbdevice_fs.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <mbedtls/ssl.h>
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/x509_crt.h>

static volatile sig_atomic_t running=1;
static void stop(int sig) { (void)sig; running=0; }
static long long now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (long long)t.tv_sec*1000+t.tv_nsec/1000000; }
static void logmsg(const char *s) { time_t t=time(NULL); struct tm tm; localtime_r(&t,&tm); char stamp[32]; strftime(stamp,sizeof stamp,"%Y-%m-%d %H:%M:%S",&tm); printf("%s %s\n",stamp,s); fflush(stdout); }
static int number_file(const char *p,int base) { FILE *f=fopen(p,"r"); if(!f)return -1; char b[32]; int n=fgets(b,sizeof b,f)?(int)strtol(b,NULL,base):-1; fclose(f); return n; }
static int usb_open(void) {
 glob_t g={0}; int fd=-1; if(glob("/sys/bus/usb/devices/*/idVendor",0,NULL,&g))return -1;
 for(size_t i=0;i<g.gl_pathc;i++) {
  char base[512],p[600]; snprintf(base,sizeof base,"%s",g.gl_pathv[i]); char *s=strrchr(base,'/'); if(!s)continue; *s=0;
  snprintf(p,sizeof p,"%s/idVendor",base); if(number_file(p,16)!=0x0a12)continue;
  snprintf(p,sizeof p,"%s/idProduct",base); if(number_file(p,16)!=1)continue;
  snprintf(p,sizeof p,"%s/busnum",base); int bus=number_file(p,10);
  snprintf(p,sizeof p,"%s/devnum",base); int dev=number_file(p,10);
  if(bus<0||dev<0)continue; snprintf(p,sizeof p,"/dev/bus/usb/%03d/%03d",bus,dev);
  fd=open(p,O_RDWR|O_CLOEXEC); if(fd<0)continue; unsigned iface=0;
  if(ioctl(fd,USBDEVFS_CLAIMINTERFACE,&iface)<0) {
   struct usbdevfs_disconnect_claim dc={0}; dc.interface=0;
   if(ioctl(fd,USBDEVFS_DISCONNECT_CLAIM,&dc)<0) {close(fd);fd=-1;continue;}
  }
  break;
 }
 globfree(&g); return fd;
}
static void usb_close(int fd) { if(fd>=0) {unsigned iface=0;ioctl(fd,USBDEVFS_RELEASEINTERFACE,&iface);close(fd);} }
static int usb_command(int fd,const unsigned char *p,int n) {
 struct usbdevfs_ctrltransfer c={.bRequestType=0x20,.bRequest=0,.wValue=0,.wIndex=0,.wLength=n,.timeout=2000,.data=(void*)p};
 return ioctl(fd,USBDEVFS_CONTROL,&c)==n?0:-1;
}
/* Reap before freeing any URB, including cancellation paths. */
static int interrupt_read(int fd,unsigned char *buf,int capacity,int timeout) {
 struct usbdevfs_urb *u=calloc(1,sizeof *u); if(!u)return -1;
 u->type=USBDEVFS_URB_TYPE_INTERRUPT;u->endpoint=0x81;u->buffer=buf;u->buffer_length=capacity;
 if(ioctl(fd,USBDEVFS_SUBMITURB,u)<0){free(u);return -1;}
 struct pollfd p={fd,POLLOUT,0}; int result=-1; void *done=NULL;
 if(poll(&p,1,timeout)>0 && ioctl(fd,USBDEVFS_REAPURBNDELAY,&done)==0) {
  if(u->status==0)result=u->actual_length;
 } else {
  ioctl(fd,USBDEVFS_DISCARDURB,u);
  while(ioctl(fd,USBDEVFS_REAPURB,&done)<0 && errno==EINTR) {}
 }
 free(u);return result;
}
static int event_read(int fd,unsigned char *out,int capacity,int timeout) {
 int used=0;long long deadline=now_ms()+timeout;
 while(running&&now_ms()<deadline&&used<capacity) {
  int n=interrupt_read(fd,out+used,capacity-used,(int)(deadline-now_ms()));if(n<=0)return -1;used+=n;
  if(used>=2&&used>=2+out[1])return 2+out[1];
 }
 return -1;
}
static int command_response(int fd,const unsigned char *cmd,int n,unsigned char *reply) {
 if(usb_command(fd,cmd,n)<0)return -1;unsigned op=cmd[0]|cmd[1]<<8;long long end=now_ms()+2500;
 while(running&&now_ms()<end) {
  int got=event_read(fd,reply,512,(int)(end-now_ms()));if(got<0)return -1;
  if(op==0xfc00&&got>=13&&reply[0]==0xff)return got;
  if(got>=6&&reply[0]==0x0e&&(reply[3]|reply[4]<<8)==op)return got;
 }
 return -1;
}
static int usb_identity(const unsigned char target[6]) {
 for(int attempt=0;running&&attempt<40;attempt++) {
  int fd=usb_open();if(fd<0){usleep(250000);continue;}
  unsigned char reply[512],reset[]={3,12,0},readaddr[]={9,16,0};
  if(command_response(fd,reset,sizeof reset,reply)<0 || command_response(fd,readaddr,sizeof readaddr,reply)<12 || reply[5]) {usb_close(fd);usleep(250000);continue;}
  if(!memcmp(reply+6,target,6))return fd;
  /* PSKEY_BDADDR in PSRAM only. A warm reset applies it; no flash write. */
  unsigned char cmd[]={0,0xfc,25,0xc2,2,0,12,0,41,0,3,0x70,0,0,1,0,4,0,8,0,0,0,0,0,0,0,0,0};
  cmd[20]=target[2];cmd[21]=0;cmd[22]=target[0];cmd[23]=target[1];cmd[24]=target[3];cmd[25]=0;cmd[26]=target[4];cmd[27]=target[5];
  if(command_response(fd,cmd,sizeof cmd,reply)<13 || reply[0]!=0xff || reply[11] || reply[12]) {usb_close(fd);usleep(250000);continue;}
  unsigned char warm[]={0,0xfc,19,0xc2,2,0,9,0,42,0,2,0x40,0,0,0,0,0,0,0,0,0,0};
  usb_command(fd,warm,sizeof warm);usb_close(fd);logmsg("Applying RAM-only controller identity; waiting for UB400 reset");sleep(1);
 }
 return -1;
}
struct frame {struct frame *next;size_t len,pos;unsigned char data[4100];};
struct stream {struct usbdevfs_urb *urb;unsigned char data[4096],pending[8192];size_t used;int type;};
static int enqueue(struct frame **head,struct frame **tail,size_t *queued,const unsigned char *p,size_t n) {
 if(n>4096||*queued+n>1048576)return -1;struct frame *f=calloc(1,sizeof *f);if(!f)return -1;
 f->data[0]=n>>8;f->data[1]=n&255;if(n)memcpy(f->data+2,p,n);f->len=n+2;
 if(*tail)(*tail)->next=f;else *head=f;*tail=f;*queued+=f->len;return 0;
}
static int usb_stream_init(int fd,struct stream *s,int type,int ep) {
 memset(s,0,sizeof *s);s->type=type;s->urb=calloc(1,sizeof *s->urb);if(!s->urb)return -1;
 s->urb->type=type==4?USBDEVFS_URB_TYPE_INTERRUPT:USBDEVFS_URB_TYPE_BULK;s->urb->endpoint=ep;s->urb->buffer=s->data;s->urb->buffer_length=sizeof s->data;s->urb->usercontext=s;
 return ioctl(fd,USBDEVFS_SUBMITURB,s->urb);
}
static int relay(mbedtls_ssl_context *ssl,int net,int usb) {
 struct stream ev={0},acl={0};struct frame *head=NULL,*tail=NULL;size_t queued=0;unsigned char incoming[32768];size_t used=0;int result=-1,want_write=0;
 if(usb_stream_init(usb,&ev,4,0x81)<0||usb_stream_init(usb,&acl,2,0x82)<0)goto done;
 while(running) {
  struct usbdevfs_urb *u=NULL;
  while(ioctl(usb,USBDEVFS_REAPURBNDELAY,&u)==0) {
   struct stream *s=u->usercontext;if(u->status||u->actual_length<0)goto done;
   if(s->used+(size_t)u->actual_length>sizeof s->pending)goto done;
   memcpy(s->pending+s->used,s->data,u->actual_length);s->used+=u->actual_length;
   int header=s->type==4?2:4;
   while(s->used>=(size_t)header) {
    size_t n=s->type==4?2+s->pending[1]:4+(s->pending[2]|s->pending[3]<<8);
    if(n>4095)goto done;if(s->used<n)break;unsigned char packet[4096];packet[0]=s->type;memcpy(packet+1,s->pending,n);
    if(enqueue(&head,&tail,&queued,packet,n+1)<0)goto done;
    if(s->type==4&&s->pending[0]==4)logmsg("Incoming Bluetooth connection request");
    memmove(s->pending,s->pending+n,s->used-n);s->used-=n;
   }
   if(ioctl(usb,USBDEVFS_SUBMITURB,u)<0)goto done;
  }
  if(errno!=EAGAIN&&errno!=EWOULDBLOCK)goto done;
  if(head) {
   int n=mbedtls_ssl_write(ssl,head->data+head->pos,head->len-head->pos);
   if(n>0){head->pos+=n;if(head->pos==head->len){struct frame *f=head;head=f->next;if(!head)tail=NULL;queued-=f->len;free(f);}want_write=0;}
   else if(n==MBEDTLS_ERR_SSL_WANT_WRITE)want_write=1;
   else if(n!=MBEDTLS_ERR_SSL_WANT_READ)goto done;
  }
  struct pollfd p[2]={{net,POLLIN|((head||want_write)?POLLOUT:0),0},{usb,POLLOUT,0}};
  if(mbedtls_ssl_check_pending(ssl))p[0].revents=POLLIN;
  else if(poll(p,2,1000)<0){if(errno==EINTR)continue;goto done;}
  if(p[0].revents&(POLLERR|POLLHUP|POLLNVAL))goto done;
  if(!(p[0].revents&POLLIN)&&!mbedtls_ssl_check_pending(ssl))continue;
  int n=mbedtls_ssl_read(ssl,incoming+used,sizeof incoming-used);
  if(n==MBEDTLS_ERR_SSL_WANT_READ)continue;if(n==MBEDTLS_ERR_SSL_WANT_WRITE){want_write=1;continue;}if(n<=0)goto done;
  used+=n;
  while(used>=2) {
   size_t length=(incoming[0]<<8)|incoming[1];if(length>4096)goto done;if(used<length+2)break;
   if(!length) {if(enqueue(&head,&tail,&queued,NULL,0)<0)goto done;memmove(incoming,incoming+2,used-2);used-=2;continue;}
   unsigned char *h4=incoming+2;
   if(h4[0]==1) {
    if(length<4 || length!=(size_t)h4[3]+4)goto done;
    unsigned op=h4[1]|h4[2]<<8;
    if(op==0x0c2f) { /* VHCI forces audio flow control; this relay carries ACL only. */
     unsigned char event[]={4,14,4,1,h4[1],h4[2],0};if(enqueue(&head,&tail,&queued,event,sizeof event)<0)goto done;
    } else if(usb_command(usb,h4+1,length-1)<0)goto done;
   } else if(h4[0]==2) {
    if(length<5||length!=(size_t)(h4[3]|h4[4]<<8)+5)goto done;
    struct usbdevfs_bulktransfer b={.ep=2,.len=length-1,.timeout=2000,.data=h4+1};if(ioctl(usb,USBDEVFS_BULK,&b)!=(int)length-1)goto done;
   } else goto done;
   memmove(incoming,incoming+length+2,used-length-2);used-=length+2;
  }
 }
 result=0;
done:
 if(ev.urb)ioctl(usb,USBDEVFS_DISCARDURB,ev.urb);if(acl.urb)ioctl(usb,USBDEVFS_DISCARDURB,acl.urb);
 /* Closing usb synchronously cancels pending URBs before their storage is freed. */
 usb_close(usb);free(ev.urb);free(acl.urb);while(head){struct frame *f=head;head=f->next;free(f);}return result;
}
int main(int argc,char **argv) {
 const char *dir=argc>1?argv[1]:"/data/adb/wiimote-relay";int port=argc>2?atoi(argv[2]):18765;const char *address=argc>3?argv[3]:"94:58:CB:DA:71:16";
 if(port<1||port>65535)return 2;unsigned int a[6];if(sscanf(address,"%x:%x:%x:%x:%x:%x",a,a+1,a+2,a+3,a+4,a+5)!=6)return 2;
 unsigned char target[6];for(int i=0;i<6;i++){if(a[i]>255)return 2;target[5-i]=a[i];}
 char p[600];snprintf(p,sizeof p,"%s/service.lock",dir);int lock=open(p,O_CREAT|O_RDWR,0600);if(lock<0||flock(lock,LOCK_EX|LOCK_NB)<0)return 3;
 signal(SIGINT,stop);signal(SIGTERM,stop);signal(SIGPIPE,SIG_IGN);setvbuf(stdout,NULL,_IOLBF,0);
 mbedtls_ssl_config conf;mbedtls_ssl_config_init(&conf);mbedtls_x509_crt ca,cert;mbedtls_x509_crt_init(&ca);mbedtls_x509_crt_init(&cert);mbedtls_pk_context key;mbedtls_pk_init(&key);
 mbedtls_entropy_context entropy;mbedtls_entropy_init(&entropy);mbedtls_ctr_drbg_context rng;mbedtls_ctr_drbg_init(&rng);
 if(mbedtls_ctr_drbg_seed(&rng,mbedtls_entropy_func,&entropy,(const unsigned char*)"wiimote-onn",11))return 4;
 snprintf(p,sizeof p,"%s/tls/ca.crt",dir);if(mbedtls_x509_crt_parse_file(&ca,p))return 4;
 snprintf(p,sizeof p,"%s/tls/server.crt",dir);if(mbedtls_x509_crt_parse_file(&cert,p))return 4;
 snprintf(p,sizeof p,"%s/tls/server.key",dir);if(mbedtls_pk_parse_keyfile(&key,p,NULL,mbedtls_ctr_drbg_random,&rng))return 4;
 if(mbedtls_ssl_config_defaults(&conf,MBEDTLS_SSL_IS_SERVER,MBEDTLS_SSL_TRANSPORT_STREAM,MBEDTLS_SSL_PRESET_DEFAULT))return 4;
 mbedtls_ssl_conf_min_tls_version(&conf,MBEDTLS_SSL_VERSION_TLS1_2);mbedtls_ssl_conf_max_tls_version(&conf,MBEDTLS_SSL_VERSION_TLS1_2);
 mbedtls_ssl_conf_authmode(&conf,MBEDTLS_SSL_VERIFY_REQUIRED);mbedtls_ssl_conf_ca_chain(&conf,&ca,NULL);mbedtls_ssl_conf_rng(&conf,mbedtls_ctr_drbg_random,&rng);mbedtls_ssl_conf_read_timeout(&conf,10000);
 if(mbedtls_ssl_conf_own_cert(&conf,&cert,&key))return 4;
 int listener=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0),yes=1;setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof yes);
 struct sockaddr_in local={.sin_family=AF_INET,.sin_port=htons(port),.sin_addr.s_addr=htonl(INADDR_ANY)};
 if(bind(listener,(struct sockaddr*)&local,sizeof local)||listen(listener,4)){perror("listen");return 5;}
 logmsg("ONN USB relay listening; mutual TLS authentication required");
 while(running) {
  struct pollfd pfd={listener,POLLIN,0};if(poll(&pfd,1,1000)<=0)continue;
  mbedtls_net_context net;mbedtls_net_init(&net);net.fd=accept(listener,NULL,NULL);if(net.fd<0)continue;
  setsockopt(net.fd,IPPROTO_TCP,TCP_NODELAY,&yes,sizeof yes);setsockopt(net.fd,SOL_SOCKET,SO_KEEPALIVE,&yes,sizeof yes);
  int idle=20,intvl=5,count=3;setsockopt(net.fd,IPPROTO_TCP,TCP_KEEPIDLE,&idle,sizeof idle);setsockopt(net.fd,IPPROTO_TCP,TCP_KEEPINTVL,&intvl,sizeof intvl);setsockopt(net.fd,IPPROTO_TCP,TCP_KEEPCNT,&count,sizeof count);
  mbedtls_ssl_context ssl;mbedtls_ssl_init(&ssl);if(mbedtls_ssl_setup(&ssl,&conf)){mbedtls_net_free(&net);continue;}
  mbedtls_ssl_set_bio(&ssl,&net,mbedtls_net_send,mbedtls_net_recv,mbedtls_net_recv_timeout);
  int r=mbedtls_ssl_handshake(&ssl);
  if(!r && !mbedtls_ssl_get_verify_result(&ssl)) {
   logmsg("Authenticated Unraid relay client");int fd=usb_identity(target);
   if(fd>=0) {
    unsigned char hello[10]={'W','M','R','1'};memcpy(hello+4,target,6);
    if(mbedtls_ssl_write(&ssl,hello,sizeof hello)==sizeof hello) {
     logmsg("UB400 ready with stable Nintendo identity");mbedtls_net_set_nonblock(&net);mbedtls_ssl_set_bio(&ssl,&net,mbedtls_net_send,mbedtls_net_recv,NULL);relay(&ssl,net.fd,fd);
    } else usb_close(fd);
   } else logmsg("UB400 unavailable or identity setup failed; client may reconnect");
  } else logmsg("Rejected TLS client or handshake failure");
  mbedtls_ssl_free(&ssl);mbedtls_net_free(&net);logmsg("Relay session ended");
 }
 close(listener);return 0;
}
