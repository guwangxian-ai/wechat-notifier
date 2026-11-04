// App-scoped Accessibility and private process-directed events. No global input.
#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <Vision/Vision.h>
#import <CoreML/CoreML.h>
#import <CommonCrypto/CommonDigest.h>
#include <signal.h>
#include <unistd.h>
#include <dlfcn.h>

static volatile sig_atomic_t capturePhase=0;
static void captureDeadline(int signalNumber) {
    (void)signalNumber;
    // Async-signal-safe hard deadline; no screenshot, child process, or private text.
    static const char windows[]="{\"error\":\"CAPTURE_TIMEOUT\",\"message\":\"后台窗口列表读取超时，已停止本次检查。\"}";
    static const char image[]="{\"error\":\"CAPTURE_TIMEOUT\",\"message\":\"后台窗口图像读取超时，已停止本次检查。\"}";
    static const char ocr[]="{\"error\":\"OCR_TIMEOUT\",\"message\":\"窗口文字识别超时，已停止本次检查。\"}";
    if(capturePhase==2) write(STDOUT_FILENO,image,sizeof(image)-1);
    else if(capturePhase==3) write(STDOUT_FILENO,ocr,sizeof(ocr)-1);
    else write(STDOUT_FILENO,windows,sizeof(windows)-1);
    _exit(0);
}

static void fail(NSString *code, NSString *message) {
    @throw [NSException exceptionWithName:code reason:message userInfo:nil];
}
static id value(AXUIElementRef el, CFStringRef key) {
    CFTypeRef result=NULL;
    AXUIElementCopyAttributeValue(el,key,&result);
    return CFBridgingRelease(result);
}
static BOOL writable(AXUIElementRef el, CFStringRef key) {
    Boolean result=false;
    return AXUIElementIsAttributeSettable(el,key,&result)==kAXErrorSuccess && result;
}
static CGRect frame(AXUIElementRef el) {
    CGPoint position=CGPointZero; CGSize size=CGSizeZero;
    id p=value(el,kAXPositionAttribute), s=value(el,kAXSizeAttribute);
    if (p && CFGetTypeID((__bridge CFTypeRef)p)==AXValueGetTypeID())
        AXValueGetValue((__bridge AXValueRef)p,kAXValueCGPointType,&position);
    if (s && CFGetTypeID((__bridge CFTypeRef)s)==AXValueGetTypeID())
        AXValueGetValue((__bridge AXValueRef)s,kAXValueCGSizeType,&size);
    return CGRectMake(position.x,position.y,size.width,size.height);
}
static NSArray *children(AXUIElementRef el) {
    id list=value(el,kAXChildrenAttribute);
    return [list isKindOfClass:NSArray.class] ? list : @[];
}
static BOOL mainTitle(id title) {
    return [@[@"微信",@"WeChat",@"Weixin"] containsObject:title?:@""];
}
static CGRect normalizedRect(NSDictionary *dict) {
    if (![dict isKindOfClass:NSDictionary.class]) fail(@"BAD_REGION",@"窗口区域无效。");
    CGRect rect=CGRectMake([dict[@"x"] doubleValue],[dict[@"y"] doubleValue],
                          [dict[@"w"] doubleValue],[dict[@"h"] doubleValue]);
    if (!isfinite(rect.origin.x)||!isfinite(rect.origin.y)||!isfinite(rect.size.width)||!isfinite(rect.size.height)||
        rect.size.width<=0||rect.size.height<=0||!CGRectContainsRect(CGRectMake(0,0,1,1),rect))
        fail(@"BAD_REGION",@"窗口区域超出主窗口。");
    return rect;
}
static NSDictionary *rectJSON(CGRect r) {
    return @{@"x":@(r.origin.x),@"y":@(r.origin.y),@"w":@(r.size.width),@"h":@(r.size.height)};
}
static double snapPixel(double x) {
    return fabs(x-round(x))<1e-7?round(x):x;
}
static CGRect pixelRect(CGImageRef image,CGRect rect) {
    // Normalized integer pixel rectangles must round-trip without gaining a
    // column/row through floating point error after a window resize.
    double x=snapPixel(rect.origin.x*CGImageGetWidth(image)),y=snapPixel(rect.origin.y*CGImageGetHeight(image));
    double right=snapPixel(CGRectGetMaxX(rect)*CGImageGetWidth(image));
    double bottom=snapPixel(CGRectGetMaxY(rect)*CGImageGetHeight(image));
    return CGRectMake(floor(x),floor(y),ceil(right)-floor(x),ceil(bottom)-floor(y));
}
static NSArray *recognize(CGImageRef image,CGRect region) {
    CGRect pixels=pixelRect(image,region);
    CGImageRef crop=CGImageCreateWithImageInRect(image,pixels);
    if (!crop) fail(@"CAPTURE_FAILED",@"窗口区域无法读取。");
    NSMutableArray *items=[NSMutableArray array];
    @try {
        VNRecognizeTextRequest *request=[VNRecognizeTextRequest new];
        request.recognitionLevel=VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection=NO; request.recognitionLanguages=@[@"zh-Hans",@"en-US"];
        NSDictionary *devices=[request supportedComputeStageDevicesAndReturnError:NULL];
        for (VNComputeStage stage in devices) for (id<MLComputeDeviceProtocol> device in devices[stage]) {
            if ([(NSObject *)device isKindOfClass:MLCPUComputeDevice.class]) {
                [request setComputeDevice:device forComputeStage:stage]; break;
            }
        }
        VNImageRequestHandler *handler=[[VNImageRequestHandler alloc]initWithCGImage:crop options:@{}];
        if (![handler performRequests:@[request] error:NULL]) fail(@"OCR_FAILED",@"微信窗口文字识别失败。");
        double width=CGImageGetWidth(image),height=CGImageGetHeight(image);
        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *text=[observation topCandidates:1].firstObject;
            if (!text) continue;
            CGRect box=observation.boundingBox;
            CGRect mapped=CGRectMake((pixels.origin.x+box.origin.x*pixels.size.width)/width,
                (pixels.origin.y+(1-box.origin.y-box.size.height)*pixels.size.height)/height,
                box.size.width*pixels.size.width/width,box.size.height*pixels.size.height/height);
            NSMutableDictionary *item=[rectJSON(mapped) mutableCopy];
            item[@"text"]=text.string;item[@"confidence"]=@(text.confidence);
            NSRange members=[text.string rangeOfString:@"[（(][0-9]+[)）]$" options:NSRegularExpressionSearch];
            if (members.location!=NSNotFound && members.location>0) {
                VNRectangleObservation *name=[text boundingBoxForRange:NSMakeRange(0,members.location) error:NULL];
                CGRect b=name.boundingBox;
                item[@"name_box"]=rectJSON(CGRectMake((pixels.origin.x+b.origin.x*pixels.size.width)/width,
                    (pixels.origin.y+(1-b.origin.y-b.size.height)*pixels.size.height)/height,
                    b.size.width*pixels.size.width/width,b.size.height*pixels.size.height/height));
            }
            [items addObject:item];
        }
    } @finally {CGImageRelease(crop);}
    return items;
}
static NSDictionary *imageInfo(CGImageRef image,CGRect region,BOOL preview,BOOL profileRows) {
    CGImageRef crop=CGImageCreateWithImageInRect(image,pixelRect(image,region));
    if (!crop) fail(@"CAPTURE_FAILED",@"窗口区域无法读取。");
    size_t w=CGImageGetWidth(crop),h=CGImageGetHeight(crop);
    CGColorSpaceRef color=CGColorSpaceCreateDeviceRGB();
    CGContextRef context=CGBitmapContextCreate(NULL,w,h,8,w*4,color,kCGImageAlphaPremultipliedLast|kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(color);
    if (!context) {CGImageRelease(crop);fail(@"CAPTURE_FAILED",@"无法核对窗口图像。");}
    CGContextDrawImage(context,CGRectMake(0,0,w,h),crop);
    unsigned char *data=CGBitmapContextGetData(context),digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data,(CC_LONG)(w*h*4),digest);
    NSMutableString *hash=[NSMutableString string];for(unsigned int i=0;i<sizeof(digest);i++)[hash appendFormat:@"%02x",digest[i]];
    // Strictly uniform empty editor, sampled away from its border. The caller
    // blurs the editor first so a caret is never mistaken for an existing draft.
    size_t ink=0,green=0,minX=w,minY=h,maxX=0,maxY=0,rows[8]={0},columns[8]={0};
    int r=data[0],g=data[1],b=data[2];
    for(size_t i=0;i<w*h;i++) {
        if(abs((int)data[i*4]-r)>12||abs((int)data[i*4+1]-g)>12||abs((int)data[i*4+2]-b)>12) {
            ink++;size_t x=i%w,y=i/w;minX=MIN(minX,x);maxX=MAX(maxX,x);minY=MIN(minY,y);maxY=MAX(maxY,y);
            columns[MIN(7,x*8/w)]++;rows[MIN(7,y*8/h)]++;
        }
        if(data[i*4+1]>130 && data[i*4+1]>data[i*4]+25 && data[i*4+1]>data[i*4+2]+25)green++;
    }
    NSMutableDictionary *result=[@{@"hash":hash,@"blank":@(ink==0),@"ink_pixels":@(ink),@"green_ratio":@((double)green/(w*h))} mutableCopy];
    NSMutableArray *rowCounts=[NSMutableArray array],*columnCounts=[NSMutableArray array];
    for(NSUInteger i=0;i<8;i++){[rowCounts addObject:@(rows[i])];[columnCounts addObject:@(columns[i])];}
    result[@"pixel_size"]=@[@(w),@(h)];result[@"reference_rgb"]=@[@(r),@(g),@(b)];
    result[@"ink_rows"]=rowCounts;result[@"ink_columns"]=columnCounts;
    result[@"ink_bounds"]=ink?@[@(minX),@(minY),@(maxX-minX+1),@(maxY-minY+1)]:NSNull.null;
    if(profileRows) {
        NSMutableArray *profile=[NSMutableArray array];
        for(size_t y=0;y<h;y+=8) {
            double lowest=255,highest=0,flat=256;
            for(size_t row=y;row<MIN(y+8,h);row++) {
                double sum=0;int minimum=255,maximum=0;
                for(size_t x=0;x<w;x++) {
                    size_t index=(row*w+x)*4;int gray=(data[index]+data[index+1]+data[index+2])/3;
                    sum+=gray;minimum=MIN(minimum,gray);maximum=MAX(maximum,gray);
                }
                double mean=sum/w;lowest=MIN(lowest,mean);highest=MAX(highest,mean);
                if(maximum-minimum<=5)flat=MIN(flat,mean);
            }
            [profile addObject:@[@(region.origin.y+(double)y/CGImageGetHeight(image)),@(round(lowest)),@(round(highest)),flat<=255?@(round(flat)):NSNull.null]];
        }
        result[@"row_profile"]=profile;
    }
    if (preview) {
        NSBitmapImageRep *bitmap=[[NSBitmapImageRep alloc]initWithCGImage:crop];
        NSData *png=[bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        result[@"preview"]=[@"data:image/png;base64," stringByAppendingString:[png base64EncodedStringWithOptions:0]];
    }
    CGContextRelease(context);CGImageRelease(crop);return result;
}
static NSDictionary *matchHeader(CGImageRef image,NSDictionary *identity) {
    if (![identity isKindOfClass:NSDictionary.class]) fail(@"BAD_REGION",@"标题校验参数无效。");
    NSString *hash=identity[@"hash"];
    NSArray *size=identity[@"pixel_size"],*origin=identity[@"origin"];
    if (![hash isKindOfClass:NSString.class] || [hash rangeOfString:@"^[a-f0-9]{64}$" options:NSRegularExpressionSearch].location==NSNotFound ||
        ![size isKindOfClass:NSArray.class] || size.count!=2 || ![origin isKindOfClass:NSArray.class] || origin.count!=2)
        fail(@"BAD_REGION",@"标题校验参数无效。");
    for(id value in [size arrayByAddingObjectsFromArray:origin])
        if(![value isKindOfClass:NSNumber.class] || !isfinite([value doubleValue])) fail(@"BAD_REGION",@"标题校验参数无效。");
    double width=CGImageGetWidth(image),height=CGImageGetHeight(image);
    double w=[size[0] doubleValue],h=[size[1] doubleValue];
    double ox=[origin[0] doubleValue],oy=[origin[1] doubleValue];
    if(w<8||w>1600||h<8||h>160||w!=round(w)||h!=round(h)||ox<0||ox>width||oy<0||oy>height*.18)
        fail(@"BAD_REGION",@"标题校验区域无效。");
    // OCR supplies a bounded location proposal, never an identity decision.
    // Match exact pixels (including emoji) within +/- 3 physical pixels only.
    NSDictionary *found=nil;
    for(int dy=-3;dy<=3;dy++) for(int dx=-3;dx<=3;dx++) {
        CGRect p=CGRectMake(round(ox)+dx,round(oy)+dy,w,h);
        if(p.origin.x<0||p.origin.y<0||CGRectGetMaxX(p)>width||CGRectGetMaxY(p)>height*.18)continue;
        CGRect r=CGRectMake(p.origin.x/width,p.origin.y/height,w/width,h/height);
        NSDictionary *info=imageInfo(image,r,NO,NO);
        if([info[@"hash"] isEqual:hash] && ![info[@"blank"] boolValue]) {
            if(found)return @{@"matched":@NO};
            found=@{@"matched":@YES,@"rect":rectJSON(r),@"hash":hash};
        }
    }
    return found?:@{@"matched":@NO};
}
static NSDictionary *withTitleInk(CGImageRef image,NSDictionary *item,CGSize logicalSize) {
    // Vision's glyph bounds may drift several points although the actual
    // title pixels did not move. Use the first painted pixel as the anchor.
    CGRect box=normalizedRect(item[@"name_box"]?:item);
    double px=3/logicalSize.width,py=3/logicalSize.height;
    CGRect region=CGRectIntersection(CGRectMake(0,0,1,.18),CGRectInset(box,-px,-py));
    NSDictionary *info=imageInfo(image,region,NO,NO);
    NSArray *ink=info[@"ink_bounds"];
    if(![ink isKindOfClass:NSArray.class]||ink.count!=4)return item;
    CGRect crop=pixelRect(image,region);
    double w=CGImageGetWidth(image),h=CGImageGetHeight(image);
    NSMutableDictionary *result=[item mutableCopy];
    result[@"ink_box"]=rectJSON(CGRectMake((crop.origin.x+[ink[0] doubleValue])/w,
        (crop.origin.y+[ink[1] doubleValue])/h,[ink[2] doubleValue]/w,[ink[3] doubleValue]/h));
    return result;
}
static NSDictionary *health(void) {
    NSArray *apps=[NSRunningApplication runningApplicationsWithBundleIdentifier:@"com.tencent.xinWeChat"];
    NSRunningApplication *app=apps.count==1?apps.firstObject:nil;
    NSDictionary *session=CFBridgingRelease(CGSessionCopyCurrentDictionary());
    BOOL locked=[session[@"CGSSessionScreenIsLocked"] boolValue] ||
        (session[(__bridge id)kCGSessionOnConsoleKey] && ![session[(__bridge id)kCGSessionOnConsoleKey] boolValue]);
    NSBundle *bundle=app.bundleURL?[NSBundle bundleWithURL:app.bundleURL]:nil;
    return @{@"accessibility":AXIsProcessTrusted()?@YES:@NO,
        @"screen_recording":CGPreflightScreenCaptureAccess()?@YES:@NO,
        @"screen_locked":locked?@YES:@NO,@"wechat_running":app?@YES:@NO,
        @"wechat_version":[bundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"]?:@""};
}
static void waitFor(dispatch_semaphore_t completion, NSString *stage) {
    NSTimeInterval deadline=NSProcessInfo.processInfo.systemUptime+4;
    while (dispatch_semaphore_wait(completion,DISPATCH_TIME_NOW)!=0) {
        if (NSProcessInfo.processInfo.systemUptime>deadline)
            fail(@"CAPTURE_TIMEOUT",[NSString stringWithFormat:@"后台窗口读取超时（%@），已停止本次检查。",stage]);
        CFRunLoopRunInMode(kCFRunLoopDefaultMode,0.01,true);
    }
}

@interface Background : NSObject
@property(nonatomic,strong) NSRunningApplication *app;
@property(nonatomic,strong) id root;
@property(nonatomic,strong) id window;
@property(nonatomic,strong) NSDictionary *windowInfo;
@property(nonatomic,strong) NSMutableArray *editors;
@property(nonatomic,strong) NSMutableArray *searchFields;
@property BOOL limited;
@property NSUInteger nodes;
@property BOOL readonlyCapture;
@end

@implementation Background
- (void)guard {
    NSDictionary *session=CFBridgingRelease(CGSessionCopyCurrentDictionary());
    if ([session[@"CGSSessionScreenIsLocked"] boolValue] ||
        (session[(__bridge id)kCGSessionOnConsoleKey] && ![session[(__bridge id)kCGSessionOnConsoleKey] boolValue]))
        fail(@"NO_DESKTOP",@"桌面已锁定，后台任务已保留。");
    if (!AXIsProcessTrusted()) fail(@"ACCESSIBILITY_PERMISSION",@"辅助功能尚未授权。");
    if (!self.app || self.app.terminated) fail(@"NO_WECHAT",@"微信未运行。");
    if (!self.readonlyCapture && NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier==self.app.processIdentifier)
        fail(@"WECHAT_IN_USE",@"微信正在前台使用，请将它留在后台后重试。");
    if (self.app.hidden) fail(@"WINDOW_HIDDEN",@"微信已隐藏；后台模式需要主窗口保持打开，可由其他应用遮挡。");
}
- (instancetype)init {
    self=[super init];
    if (self) {
        NSArray *apps=[NSRunningApplication runningApplicationsWithBundleIdentifier:@"com.tencent.xinWeChat"];
        if (apps.count!=1) fail(@"NO_WECHAT",@"无法唯一识别个人微信进程。");
        self.app=apps.firstObject;
        self.root=CFBridgingRelease(AXUIElementCreateApplication(self.app.processIdentifier));
        AXUIElementSetMessagingTimeout((__bridge AXUIElementRef)self.root,0.2);
    }
    return self;
}
- (void)inspect {
    [self guard];
    AXUIElementRef root=(__bridge AXUIElementRef)self.root;
    NSMutableArray *windows=[NSMutableArray array];
    for (id candidate in value(root,kAXWindowsAttribute)) {
        AXUIElementRef win=(__bridge AXUIElementRef)candidate;
        if (mainTitle(value(win,kAXTitleAttribute))) [windows addObject:candidate];
    }
    if (windows.count!=1) fail(@"WINDOW_UNCERTAIN",@"无法唯一识别微信主窗口。");
    self.window=windows.firstObject;
    AXUIElementRef win=(__bridge AXUIElementRef)self.window;
    if ([value(win,kAXMinimizedAttribute) boolValue])
        fail(@"WINDOW_MINIMIZED",@"微信主窗口已最小化，请恢复窗口；不需要将微信保持在前台。");
    CGRect bounds=frame(win);
    if (bounds.size.width<650 || bounds.size.height<450)
        fail(@"WINDOW_TOO_SMALL",@"微信主窗口太小，无法可靠核对目标群。");
    NSArray *all=CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly|kCGWindowListExcludeDesktopElements,kCGNullWindowID));
    NSMutableArray *matches=[NSMutableArray array];
    for (NSDictionary *info in all) {
        CGRect rect=CGRectZero;
        CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)info[(id)kCGWindowBounds],&rect);
        if ([info[(id)kCGWindowOwnerPID] intValue]==self.app.processIdentifier &&
            [info[(id)kCGWindowLayer] intValue]==0 &&
            fabs(rect.origin.x-bounds.origin.x)<2 && fabs(rect.origin.y-bounds.origin.y)<2 &&
            fabs(rect.size.width-bounds.size.width)<2 && fabs(rect.size.height-bounds.size.height)<2)
            [matches addObject:info];
    }
    if (matches.count!=1) fail(@"WINDOW_UNCERTAIN",@"微信主窗口不在当前桌面，或窗口位置无法核对。");
    self.windowInfo=matches.firstObject;
    self.editors=[NSMutableArray array]; self.searchFields=[NSMutableArray array];
    NSMutableArray *todo=[NSMutableArray arrayWithObject:self.window];
    self.nodes=0;
    NSTimeInterval start=NSProcessInfo.processInfo.systemUptime;
    while (todo.count && self.nodes<1500 && NSProcessInfo.processInfo.systemUptime-start<2) {
        id node=todo.lastObject; [todo removeLastObject]; self.nodes++;
        AXUIElementRef el=(__bridge AXUIElementRef)node;
        NSString *role=value(el,kAXRoleAttribute);
        if ([role isEqual:@"AXTextArea"] || [role isEqual:@"AXTextField"]) {
            CGRect rect=frame(el);
            id text=value(el,kAXValueAttribute);
            if ([text isKindOfClass:NSString.class] && writable(el,kAXValueAttribute) &&
                CGRectContainsRect(bounds,rect)) {
                if (rect.origin.x>bounds.origin.x+bounds.size.width*.30 &&
                    rect.origin.y>bounds.origin.y+bounds.size.height*.48) [self.editors addObject:node];
                if (rect.origin.x<bounds.origin.x+bounds.size.width*.37 &&
                    rect.origin.y<bounds.origin.y+bounds.size.height*.17) [self.searchFields addObject:node];
            }
        }
        [todo addObjectsFromArray:children(el)];
    }
    self.limited=todo.count>0;
    [self guard];
}
- (void)requireControls {
    if (self.limited || self.editors.count!=1 || self.searchFields.count!=1)
        fail(@"BACKGROUND_UNSUPPORTED",@"当前微信没有提供唯一、可读写的搜索框和聊天输入框，无法安全后台发送；不会切换前台重试。");
}
- (NSDictionary *)capture:(NSDictionary *)options {
    if (!CGPreflightScreenCaptureAccess()) fail(@"SCREEN_PERMISSION",@"屏幕录制尚未授权。");
    signal(SIGALRM,captureDeadline); capturePhase=1; alarm(40);
    NSTimeInterval start=NSProcessInfo.processInfo.systemUptime;
    // Capture only the matched WeChat window. No PNG, screenshot subprocess, or desktop image.
    dispatch_semaphore_t listed=dispatch_semaphore_create(0);
    __block SCShareableContent *content=nil;
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *result,NSError *error) {
        (void)error; content=result; dispatch_semaphore_signal(listed);
    }];
    waitFor(listed,@"window-list");
    SCWindow *target=nil;
    for (SCWindow *candidate in content.windows) {
        if (candidate.windowID==[self.windowInfo[(id)kCGWindowNumber] unsignedIntValue] &&
            candidate.owningApplication.processID==self.app.processIdentifier) target=candidate;
    }
    if (!target) fail(@"CAPTURE_FAILED",@"系统未提供可读取的微信主窗口。");
    CGRect expected=frame((__bridge AXUIElementRef)self.window);
    CGRect inspected=CGRectZero;
    CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)self.windowInfo[(id)kCGWindowBounds],&inspected);
    if(!CGRectEqualToRect(expected,target.frame)||!CGRectEqualToRect(expected,inspected))
        fail(@"WINDOW_CHANGED",@"微信窗口仍在变化，未执行输入。");
    SCContentFilter *filter=[[SCContentFilter alloc]initWithDesktopIndependentWindow:target];
    capturePhase=2;
    SCStreamConfiguration *config=[SCStreamConfiguration new];
    double scale=MIN(2.0,4096.0/MAX(target.frame.size.width,target.frame.size.height));
    config.width=(size_t)round(target.frame.size.width*scale);
    config.height=(size_t)round(target.frame.size.height*scale);
    config.scalesToFit=YES;
    config.preservesAspectRatio=YES;
    config.showsCursor=NO; config.ignoreShadowsSingleWindow=YES;
    dispatch_semaphore_t captured=dispatch_semaphore_create(0);
    __block CGImageRef image=NULL;
    [SCScreenshotManager captureImageWithFilter:filter configuration:config completionHandler:^(CGImageRef result,NSError *error) {
        (void)error; if(result)image=CGImageRetain(result); dispatch_semaphore_signal(captured);
    }];
    waitFor(captured,@"window-image");
    if (!image) fail(@"CAPTURE_FAILED",@"微信后台窗口图像无法读取。");
    NSDictionary *imageSize=@{@"width":@(CGImageGetWidth(image)),@"height":@(CGImageGetHeight(image))};
    NSArray *items=nil;NSMutableDictionary *regions=[NSMutableDictionary dictionary];
    NSMutableDictionary *snapshots=[NSMutableDictionary dictionary];
    BOOL outgoingMarker=NO;NSDictionary *headerMatch=nil;
    capturePhase=3;
    @try {
        items=recognize(image,CGRectMake(0,0,1,1));
        if ([options[@"visual"] boolValue]) {
            NSMutableArray *header=[NSMutableArray array];
            for(NSDictionary *item in recognize(image,CGRectMake(0,0,1,MIN(.18,80/target.frame.size.height))))
                [header addObject:withTitleInk(image,item,target.frame.size)];
            regions[@"header"]=header;
            regions[@"sidebar"]=recognize(image,CGRectMake(0,0,MIN(.45,360/target.frame.size.width),1));
            regions[@"lower"]=recognize(image,CGRectMake(.20,.45,.80,.55));
        }
        if(options[@"header_identity"])headerMatch=matchHeader(image,options[@"header_identity"]);
        for (NSString *name in @[@"header",@"composer"]) if (options[[name stringByAppendingString:@"_rect"]]) {
            CGRect rect=normalizedRect(options[[name stringByAppendingString:@"_rect"]]);
            if ([name isEqual:@"header"] && CGRectGetMaxY(rect)>.18) fail(@"BAD_REGION",@"群标题区域无效。");
            if ([name isEqual:@"composer"] && rect.origin.y<.45) fail(@"BAD_REGION",@"输入框区域无效。");
            snapshots[name]=imageInfo(image,rect,[name isEqual:@"header"]&&[options[@"preview"] boolValue],
                                     [name isEqual:@"composer"]&&[options[@"layout_profile"] boolValue]);
            regions[name]=recognize(image,rect);
        }
        NSString *marker=options[@"expected_marker"];
        if ([marker isKindOfClass:NSString.class] && [marker rangeOfString:@"^WN-[A-F0-9]{12}$" options:NSRegularExpressionSearch].location!=NSNotFound) {
            double bottom=options[@"composer_rect"]?normalizedRect(options[@"composer_rect"]).origin.y-.04:.76;
            for(NSDictionary *item in items) {
                NSString *text=[item[@"text"] stringByReplacingOccurrencesOfString:@"\\s+" withString:@"" options:NSRegularExpressionSearch range:NSMakeRange(0,[item[@"text"] length])];
                CGRect rect=normalizedRect(item);
                if(rect.origin.x>.20 && rect.origin.y>.12 && CGRectGetMaxY(rect)<bottom && [text containsString:marker]) {
                    NSDictionary *pixels=imageInfo(image,rect,NO,NO);
                    if([pixels[@"green_ratio"] doubleValue]>.20)outgoingMarker=YES;
                }
            }
        }
    } @finally { CGImageRelease(image); }
    alarm(0);
    [self guard];
    if(!CGRectEqualToRect(expected,frame((__bridge AXUIElementRef)self.window)))
        fail(@"WINDOW_CHANGED",@"微信窗口在读取时发生变化，未执行输入。");
    return @{@"items":items,@"regions":regions,@"snapshots":snapshots,@"outgoing_marker":@(outgoingMarker),
        @"header_match":headerMatch?:@{},
        @"window_id":self.windowInfo[(id)kCGWindowNumber],@"bounds":self.windowInfo[(id)kCGWindowBounds],
        @"image_size":imageSize,@"source_size":@{@"width":@(target.frame.size.width),@"height":@(target.frame.size.height)},
        @"capture_seconds":@(NSProcessInfo.processInfo.systemUptime-start)};
}
- (void)postKey:(CGKeyCode)key flags:(NSEventModifierFlags)flags text:(NSString *)text {
    [self guard];
    // Only the main window may receive a key. Do not redirect a Channels window.
    id focused=value((__bridge AXUIElementRef)self.root,kAXFocusedWindowAttribute);
    if (!focused || !CFEqual((__bridge CFTypeRef)focused,(__bridge CFTypeRef)self.window))
        fail(@"WECHAT_WINDOW_BUSY",@"微信的其他窗口正在使用输入焦点，任务已停止。");
    if(text.length>64)fail(@"BAD_MESSAGE",@"单个字符组合过长。");
    UniChar chars[64];[text getCharacters:chars range:NSMakeRange(0,text.length)];
    for(NSNumber *type in @[@(NSEventTypeKeyDown),@(NSEventTypeKeyUp)]) {
        NSEvent *event=[NSEvent keyEventWithType:type.unsignedIntegerValue location:NSZeroPoint modifierFlags:flags
            timestamp:NSProcessInfo.processInfo.systemUptime windowNumber:[self.windowInfo[(id)kCGWindowNumber] integerValue]
            context:nil characters:text charactersIgnoringModifiers:text isARepeat:NO keyCode:key];
        if(!event.CGEvent)fail(@"EVENT_UNAVAILABLE",@"无法创建微信窗口定向按键。");
        CGEventKeyboardSetUnicodeString(event.CGEvent,text.length,chars);
        CGEventPostToPid(self.app.processIdentifier,event.CGEvent);
    }
    [NSThread sleepForTimeInterval:0.02];
    [self guard];
}
- (void)key:(CGKeyCode)key flags:(CGEventFlags)flags {
    [self postKey:key flags:(NSEventModifierFlags)flags text:key==36?@"\r":@""];
}
- (void)typeText:(NSString *)text {
    if (![text isKindOfClass:NSString.class] || !text.length || text.length>8000)
        fail(@"BAD_MESSAGE",@"通知内容无效。");
    for(NSUInteger i=0;i<text.length;i++)if([text characterAtIndex:i]<32 && [text characterAtIndex:i]!='\n')
        fail(@"BAD_MESSAGE",@"通知含不支持的控制字符，请用空格替代制表符。");
    // WeChat 4.x consumes only the first composed character from each key event.
    // Keep the Unicode payload to one character; grouping silently truncates it.
    [text enumerateSubstringsInRange:NSMakeRange(0,text.length) options:NSStringEnumerationByComposedCharacterSequences
        usingBlock:^(NSString *part,NSRange range,NSRange enclosing,BOOL *stop) {
            (void)range;(void)enclosing;(void)stop;
            [self postKey:0xffff flags:0 text:part];
        }];
}
- (void)clickX:(double)x y:(double)y {
    [self guard];
    CGRect bounds=frame((__bridge AXUIElementRef)self.window);
    if (!isfinite(x)||!isfinite(y)||!CGRectContainsPoint(bounds,CGPointMake(x,y)))
        fail(@"BAD_COORDINATES",@"目标坐标不在微信主窗口内。");
    int64_t windowID=[self.windowInfo[(id)kCGWindowNumber] longLongValue];
    CGPoint local=CGPointMake(x-bounds.origin.x,y-bounds.origin.y);
    typedef void (*SetWindowLocation)(CGEventRef,CGPoint);
    SetWindowLocation setWindowLocation=(SetWindowLocation)dlsym(RTLD_DEFAULT,"CGEventSetWindowLocation");
    if (!setWindowLocation) fail(@"EVENT_UNAVAILABLE",@"系统未提供窗口定向事件坐标接口。");
    // NSEvent supplies window metadata omitted by CGEventCreateMouseEvent.
    // Command-click allows an inactive window to handle the click without activation.
    for (NSNumber *type in @[@(NSEventTypeLeftMouseDown),@(NSEventTypeLeftMouseUp)]) {
        NSEvent *native=[NSEvent mouseEventWithType:type.unsignedIntegerValue location:local
            modifierFlags:NSEventModifierFlagCommand timestamp:NSProcessInfo.processInfo.systemUptime
            windowNumber:windowID context:nil eventNumber:0 clickCount:1 pressure:0];
        CGEventRef event=native.CGEvent;
        if (!event) fail(@"EVENT_UNAVAILABLE",@"无法创建微信窗口定向事件。");
        CGEventSetLocation(event,CGPointMake(x,y));
        setWindowLocation(event,local);
        CGEventSetIntegerValueField(event,kCGMouseEventSubtype,3);
        CGEventSetIntegerValueField(event,kCGMouseEventButtonNumber,kCGMouseButtonLeft);
        CGEventSetIntegerValueField(event,kCGMouseEventClickState,1);
        CGEventSetIntegerValueField(event,kCGMouseEventWindowUnderMousePointer,windowID);
        CGEventSetIntegerValueField(event,kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,windowID);
        CGEventPostToPid(self.app.processIdentifier,event);
    }
    [NSThread sleepForTimeInterval:0.2];
    [self guard];
}
- (NSDictionary *)run:(NSDictionary *)request {
    NSString *action=request[@"action"];
    // A diagnostic may inspect the visible editor while the user looks at it.
    // This flag cannot relax any click, key, search, write, or send operation.
    self.readonlyCapture=[action isEqual:@"capture"] && [request[@"diagnostic"] boolValue];
    [self inspect];
    if ([action isEqual:@"capture"]) return [self capture:request];
    if ([action hasPrefix:@"visual_"]) {
        NSDictionary *expected=request[@"bounds"];
        CGRect actual=frame((__bridge AXUIElementRef)self.window);
        if (![request[@"window_id"] isEqual:self.windowInfo[(id)kCGWindowNumber]] || !expected ||
            fabs(actual.origin.x-[expected[@"X"] doubleValue])>.5 || fabs(actual.origin.y-[expected[@"Y"] doubleValue])>.5 ||
            fabs(actual.size.width-[expected[@"Width"] doubleValue])>.5 || fabs(actual.size.height-[expected[@"Height"] doubleValue])>.5)
            fail(@"WINDOW_CHANGED",@"微信窗口在核对后发生变化，已停止操作。");
        if ([action isEqual:@"visual_view"] || [action isEqual:@"visual_clear"]) {
            double x=[request[@"x"] doubleValue],y=[request[@"y"] doubleValue];
            double sx=[request[@"search_x"] doubleValue],sy=[request[@"search_y"] doubleValue];
            if(x<actual.origin.x+actual.size.width*.2||y<actual.origin.y+actual.size.height*.5||
                sx<actual.origin.x+40||sx>actual.origin.x+400||sy<actual.origin.y+10||sy>actual.origin.y+90)
                fail(@"BAD_COORDINATES",@"输入框位置无法核对。");
            [self clickX:x y:y];
            if ([action isEqual:@"visual_clear"]) {
                [self postKey:0 flags:NSEventModifierFlagCommand text:@"a"];
                [self postKey:51 flags:0 text:@"\x7f"];
            } else if ([request[@"page"] isEqual:@"end"]) {
                [self postKey:125 flags:NSEventModifierFlagCommand text:@"\uf701"];
            } else {
                NSInteger page=[request[@"page"] integerValue];
                if(page<0||page>24)fail(@"BAD_ACTION",@"输入框翻页超出限制。");
                [self postKey:126 flags:NSEventModifierFlagCommand text:@"\uf700"];
                for(NSInteger i=0;i<page;i++)[self postKey:121 flags:0 text:@"\uf72d"];
            }
            [self clickX:sx y:sy];
            return @{@"ok":@YES};
        }
        if ([action isEqual:@"visual_key"]) {
            NSString *key=request[@"key"];
            if ([key isEqual:@"home"]) [self postKey:126 flags:NSEventModifierFlagCommand text:@"\uf700"];
            else if ([key isEqual:@"end"]) [self postKey:125 flags:NSEventModifierFlagCommand text:@"\uf701"];
            else if ([key isEqual:@"page_down"]) [self postKey:121 flags:0 text:@"\uf72d"];
            else if ([key isEqual:@"select_all"]) [self postKey:0 flags:NSEventModifierFlagCommand text:@"a"];
            else if ([key isEqual:@"delete"]) [self postKey:51 flags:0 text:@"\x7f"];
            else if ([key isEqual:@"escape"]) [self postKey:53 flags:0 text:@"\x1b"];
            else fail(@"BAD_ACTION",@"不支持的窗口按键。");
            return @{@"ok":@YES};
        }
        double x=[request[@"x"] doubleValue],y=[request[@"y"] doubleValue];
        if ([action isEqual:@"visual_search"] &&
            (x<actual.origin.x+40||x>actual.origin.x+400||y<actual.origin.y+10||y>actual.origin.y+90))
            fail(@"BAD_COORDINATES",@"搜索框位置无法核对。");
        if ([action isEqual:@"visual_write"] && (x<actual.origin.x+actual.size.width*.2||y<actual.origin.y+actual.size.height*.5))
            fail(@"BAD_COORDINATES",@"输入框位置无法核对。");
        if (![@[@"visual_click",@"visual_search",@"visual_write"] containsObject:action]) fail(@"BAD_ACTION",@"不支持的窗口操作。");
        [self clickX:x y:y];
        if ([action isEqual:@"visual_search"]) {
            NSString *query=request[@"query"];
            if(![query isKindOfClass:NSString.class]||!query.length||query.length>100||
                [query rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location!=NSNotFound)fail(@"BAD_QUERY",@"群名无效。");
            [self postKey:0 flags:NSEventModifierFlagCommand text:@"a"];
            [self postKey:51 flags:0 text:@"\x7f"];
            [self typeText:query];
        } else if ([action isEqual:@"visual_write"]) [self typeText:request[@"message"]];
        return @{@"ok":@YES};
    }
    if ([action isEqual:@"focus_search"]) {
        double x=[request[@"x"] doubleValue], y=[request[@"y"] doubleValue];
        CGRect bounds=frame((__bridge AXUIElementRef)self.window);
        if (x<bounds.origin.x+40 || x>bounds.origin.x+MIN(450,bounds.size.width*.35) ||
            y<bounds.origin.y+10 || y>bounds.origin.y+100)
            fail(@"BAD_COORDINATES",@"搜索框位置无法核对。");
        [self clickX:x y:y];
        [self inspect];
        id focused=value((__bridge AXUIElementRef)self.root,kAXFocusedUIElementAttribute);
        id focusedWindow=value((__bridge AXUIElementRef)self.root,kAXFocusedWindowAttribute);
        return @{@"editor_count":@(self.editors.count),@"search_count":@(self.searchFields.count),
            @"focused_role":focused?(value((__bridge AXUIElementRef)focused,kAXRoleAttribute)?:@""):@"",
            @"main_window_focused":@(focusedWindow && CFEqual((__bridge CFTypeRef)focusedWindow,(__bridge CFTypeRef)self.window))};
    }
    if ([action isEqual:@"capabilities"]) {
        NSMutableDictionary *accessibility=[NSMutableDictionary dictionary];
        for (NSString *key in @[@"AXEnhancedUserInterface",@"AXManualAccessibility"]) {
            id current=value((__bridge AXUIElementRef)self.root,(__bridge CFStringRef)key);
            accessibility[key]=@{@"writable":@(writable((__bridge AXUIElementRef)self.root,(__bridge CFStringRef)key)),
                @"enabled":@([current isEqual:@YES])};
        }
        return @{
        @"supported":(!self.limited && self.editors.count==1 && self.searchFields.count==1)?@YES:@NO,
        @"editor_count":@(self.editors.count),@"search_count":@(self.searchFields.count),
        @"accessibility_options":accessibility,
        @"visual_protocol":@"window-unicode-v1",@"bounds":self.windowInfo[(id)kCGWindowBounds],
        @"nodes":@(self.nodes),@"limited":@(self.limited),@"window_id":self.windowInfo[(id)kCGWindowNumber]};
    }
    [self requireControls];
    if ([action isEqual:@"click"]) {
        [self clickX:[request[@"x"] doubleValue] y:[request[@"y"] doubleValue]];
        return @{@"ok":@YES};
    }
    if ([action isEqual:@"search"]) {
        NSString *query=request[@"query"];
        if (![query isKindOfClass:NSString.class] || !query.length || query.length>100)
            fail(@"BAD_QUERY",@"群名无效。");
        AXUIElementRef field=(__bridge AXUIElementRef)self.searchFields.firstObject;
        NSString *existing=value(field,kAXValueAttribute);
        if (existing.length && ![existing isEqual:query])
            fail(@"EXISTING_SEARCH",@"微信搜索框已有内容，已保留；请先清空搜索框。");
        CGRect rect=frame(field);
        [self clickX:CGRectGetMidX(rect) y:CGRectGetMidY(rect)];
        if (AXUIElementSetAttributeValue(field,kAXValueAttribute,(__bridge CFTypeRef)query)!=kAXErrorSuccess ||
            ![value(field,kAXValueAttribute) isEqual:query]) fail(@"SEARCH_WRITE_FAILED",@"后台搜索内容未能准确写入。");
        [self guard];
        return @{@"ok":@YES};
    }
    AXUIElementRef editor=(__bridge AXUIElementRef)self.editors.firstObject;
    NSString *old=value(editor,kAXValueAttribute);
    if ([action isEqual:@"read"]) return @{@"text":old};
    NSString *expected=request[@"message"];
    if (![expected isKindOfClass:NSString.class] || expected.length>8000)
        fail(@"BAD_MESSAGE",@"通知内容无效。");
    if ([action isEqual:@"write"]) {
        if (old.length) fail(@"EXISTING_DRAFT",@"目标群已有草稿，已保留原文。");
        if (AXUIElementSetAttributeValue(editor,kAXValueAttribute,(__bridge CFTypeRef)expected)!=kAXErrorSuccess ||
            ![value(editor,kAXValueAttribute) isEqual:expected]) fail(@"DRAFT_MISMATCH",@"后台草稿写入未能准确核对，未发送。");
    } else {
        if (![old isEqual:expected]) fail(@"DRAFT_MISMATCH",@"输入框内容与通知不一致，已停止操作。");
        if ([action isEqual:@"clear"]) {
            if (AXUIElementSetAttributeValue(editor,kAXValueAttribute,CFSTR(""))!=kAXErrorSuccess ||
                ![value(editor,kAXValueAttribute) isEqual:@""]) fail(@"CLEAR_FAILED",@"未能清除本次通知草稿，请核对微信。");
        } else if ([action isEqual:@"send"]) {
            if (!expected.length) fail(@"BAD_MESSAGE",@"不发送空通知。");
            CGRect rect=frame(editor);
            [self clickX:CGRectGetMidX(rect) y:CGRectGetMidY(rect)];
            id focused=value((__bridge AXUIElementRef)self.root,kAXFocusedUIElementAttribute);
            if (!focused || !CFEqual((__bridge CFTypeRef)focused,editor) ||
                ![value(editor,kAXValueAttribute) isEqual:expected])
                fail(@"COMPOSER_FOCUS_FAILED",@"微信后台输入焦点无法核对，未执行回车。");
            [self key:36 flags:0];
        } else fail(@"BAD_ACTION",@"不支持的后台操作。");
    }
    [self guard];
    return @{@"ok":@YES};
}
@end

int main(void) { @autoreleasepool {
    NSMutableDictionary *result;
    pid_t before=NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;
    CGEventRef event=CGEventCreate(NULL); CGPoint mouse=CGEventGetLocation(event); CFRelease(event);
    NSInteger clipboard=NSPasteboard.generalPasteboard.changeCount;
    @try {
        NSData *data=[NSFileHandle.fileHandleWithStandardInput readDataOfLength:32769];
        if (data.length>32768) fail(@"INPUT_TOO_LARGE",@"后台操作请求过大。");
        id request=[NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
        if (![request isKindOfClass:NSDictionary.class]) fail(@"BAD_REQUEST",@"后台操作请求无效。");
        result=[([request[@"action"] isEqual:@"health"]?health():[[Background new] run:request]) mutableCopy];
    } @catch (NSException *exception) {
        // Only known, fixed errors are safe to return. Never leak AX values.
        NSSet *codes=[NSSet setWithArray:@[@"NO_DESKTOP",@"ACCESSIBILITY_PERMISSION",@"NO_WECHAT",@"WECHAT_IN_USE",@"WINDOW_HIDDEN",@"WINDOW_UNCERTAIN",@"WINDOW_MINIMIZED",@"WINDOW_TOO_SMALL",@"BACKGROUND_UNSUPPORTED",@"WECHAT_WINDOW_BUSY",@"EVENT_UNAVAILABLE",@"BAD_COORDINATES",@"BAD_QUERY",@"EXISTING_SEARCH",@"SEARCH_WRITE_FAILED",@"BAD_MESSAGE",@"EXISTING_DRAFT",@"DRAFT_MISMATCH",@"CLEAR_FAILED",@"COMPOSER_FOCUS_FAILED",@"BAD_ACTION",@"INPUT_TOO_LARGE",@"BAD_REQUEST",@"SCREEN_PERMISSION",@"CAPTURE_TIMEOUT",@"CAPTURE_FAILED",@"OCR_FAILED",@"BAD_REGION",@"WINDOW_CHANGED"]];
        BOOL known=[codes containsObject:exception.name];
        result=[@{@"error":known?exception.name:@"BACKGROUND_ERROR",@"message":known?exception.reason:@"后台控件检查失败。"} mutableCopy];
    }
    event=CGEventCreate(NULL); CGPoint after=CGEventGetLocation(event); CFRelease(event);
    result[@"foreground_unchanged"]=(before==NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier)?@YES:@NO;
    result[@"mouse_unchanged"]=@(CGPointEqualToPoint(mouse,after));
    result[@"clipboard_unchanged"]=(clipboard==NSPasteboard.generalPasteboard.changeCount)?@YES:@NO;
    NSData *json=[NSJSONSerialization dataWithJSONObject:result options:0 error:NULL];
    [NSFileHandle.fileHandleWithStandardOutput writeData:json];
    return 0;
}}
