/* Wii / Wii U wake: authenticated ACL + power-button disconnect, before HID.
 * A powered-on console answers the HID probe and must never receive the toggle.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <signal.h>
#include <poll.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <bluetooth/bluetooth.h>
#include <bluetooth/hci.h>
#include <bluetooth/hci_lib.h>
#include <bluetooth/l2cap.h>
#include "adapter.h"

static volatile sig_atomic_t running=1;
static void stop(int sig) { (void)sig;running=0; }
static double seconds(void) {struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static int start_hid(const bdaddr_t *local,const bdaddr_t *peer) {
 int fd=socket(AF_BLUETOOTH,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,BTPROTO_L2CAP);
 if(fd<0)return -1;
 struct sockaddr_l2 a={.l2_family=AF_BLUETOOTH};bacpy(&a.l2_bdaddr,local);
 if(bind(fd,(struct sockaddr*)&a,sizeof a)<0){close(fd);return -1;}
 bacpy(&a.l2_bdaddr,peer);a.l2_psm=htobs(0x11);
 if(connect(fd,(struct sockaddr*)&a,sizeof a)<0 && errno!=EINPROGRESS){close(fd);return -1;}
 return fd;
}
/* 1 = HID connected, 0 = still pending, negative = errno. */
static int hid_result(int fd,int wait_ms) {
 struct pollfd p={fd,POLLOUT|POLLERR|POLLHUP,0};int n=poll(&p,1,wait_ms);
 if(n<0)return errno==EINTR?0:-errno;if(!n)return 0;
 int err=0;socklen_t len=sizeof err;
 if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&err,&len)<0)return -errno;
 if(err)return -err;
 if(p.revents&(POLLERR|POLLHUP|POLLNVAL))return -ECONNRESET;
 return p.revents&POLLOUT?1:0;
}
static int authenticated_acl(int dd,const bdaddr_t *peer) {
 struct hci_conn_info_req *r=calloc(1,sizeof *r+sizeof(struct hci_conn_info));
 if(!r)return 0;bacpy(&r->bdaddr,peer);r->type=ACL_LINK;
 int ok=ioctl(dd,HCIGETCONNINFO,(unsigned long)r)==0 && (r->conn_info->link_mode&HCI_LM_AUTH);
 free(r);return ok;
}
int main(int argc,char **argv) {
 if(argc<2||argc>3||bachk(argv[1])<0||(argc==3&&strcmp(argv[2],"--probe-only"))) {
  fprintf(stderr,"usage: %s <console-bdaddr> [--probe-only]\n",argv[0]);return 1;
 }
 signal(SIGTERM,stop);signal(SIGINT,stop);signal(SIGHUP,stop);setvbuf(stdout,NULL,_IOLBF,0);
 bdaddr_t peer,local;str2ba(argv[1],&peer);int result=1,fd=-1,dd=-1;
 if(set_up_device(NULL)<0)return 1;
 int id=get_selected_device_id();if(id<0||get_device_bdaddr(id,&local)<0)goto cleanup;
 dd=hci_open_dev(id);if(dd<0)goto cleanup;
 char addr[18];ba2str(&local,addr);printf("Wake probe using hci%d %s\n",id,addr);
 fd=start_hid(&local,&peer);if(fd<0){perror("Unable to start wake probe");goto cleanup;}
 double deadline=seconds()+15,authenticated_at=0;
 while(running&&seconds()<deadline) {
  int state=hid_result(fd,50);
  if(state==1){printf("WAKE_ALREADY_AWAKE\n");result=0;goto cleanup;}
  /* A refused PSM is still an answer from the running console's L2CAP host. */
  if(state==-ECONNREFUSED){printf("WAKE_ALREADY_AWAKE\n");result=0;goto cleanup;}
  if(state<0){fprintf(stderr,"Wake HID probe failed: %s\n",strerror(-state));goto cleanup;}
  if(authenticated_acl(dd,&peer)) {
   if(!authenticated_at){authenticated_at=seconds();printf("Authenticated ACL established; checking whether HID host is awake\n");}
   if(seconds()-authenticated_at>=5)break;
  }
 }
 if(!running)goto cleanup;
 if(!authenticated_at){fprintf(stderr,"No authenticated Bluetooth link; wake was not sent\n");goto cleanup;}
 if(argc==3){printf("WAKE_RECENT_REQUEST\n");result=2;goto cleanup;}
 if(!authenticated_acl(dd,&peer)){fprintf(stderr,"Authenticated link was lost before wake\n");goto cleanup;}
 /* The standby controller authenticates ACL but does not run the HID service.
  * Reason 0x15 is the real remote's special power-button operation. */
 if(power_off_host(&peer)<0){perror("Power-button wake signal failed");goto cleanup;}
 printf("WAKE_SIGNAL_SENT\n");close(fd);fd=-1;
 deadline=seconds()+35;
 while(running&&seconds()<deadline) {
  fd=start_hid(&local,&peer);
  if(fd<0){usleep(500000);continue;}
  double attempt=seconds()+2;
  while(running&&seconds()<attempt&&seconds()<deadline) {
   int state=hid_result(fd,100);
   if(state==1){printf("WAKE_CONFIRMED\n");result=0;goto cleanup;}
   if(state<0)break;
  }
  close(fd);fd=-1;usleep(500000);
 }
 printf("WAKE_NOT_CONFIRMED\n");result=2;
cleanup:
 if(fd>=0)close(fd);if(dd>=0)hci_close_dev(dd);restore_device();return result;
}
