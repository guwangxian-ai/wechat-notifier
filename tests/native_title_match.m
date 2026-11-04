// Synthetic pixels only. This test never connects to or operates WeChat.
#define main backgroundBridgeMain
#include "../notifier/macos/background.m"
#undef main
#include <assert.h>

static CGImageRef testImage(size_t width, size_t height, int x, int y) {
    CGColorSpaceRef color=CGColorSpaceCreateDeviceRGB();
    CGContextRef c=CGBitmapContextCreate(NULL,width,height,8,width*4,color,kCGImageAlphaPremultipliedLast|kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(color);
    unsigned char *p=CGBitmapContextGetData(c);
    memset(p,255,width*height*4);
    for(int dy=0;dy<26;dy++) for(int dx=0;dx<110;dx++) {
        size_t i=((y+dy)*width+x+dx)*4;
        p[i]=(dx*17+dy*31)%230; p[i+1]=(dx*7+dy*11)%230; p[i+2]=(dx*3+dy*13)%230;
    }
    CGImageRef image=CGBitmapContextCreateImage(c);CGContextRelease(c);return image;
}
int main(void) { @autoreleasepool {
    CGImageRef small=testImage(1000,800,310,25), large=testImage(1600,1000,510,45);
    NSDictionary *reference=imageInfo(small,CGRectMake(.3,.025,.13,.06),NO,NO);
    NSDictionary *tight=@{@"x":@.310,@"y":@.03125,@"w":@.110,@"h":@.0325};
    NSDictionary *drifted=@{@"x":@.299,@"y":@.020,@"w":@.140,@"h":@.060};
    NSDictionary *a=withTitleInk(small,tight,CGSizeMake(1000,800));
    NSDictionary *b=withTitleInk(small,drifted,CGSizeMake(1000,800));
    assert([a[@"ink_box"] isEqual:b[@"ink_box"]]);
    assert(fabs([a[@"ink_box"][@"x"] doubleValue]-.310)<1e-9);
    NSMutableDictionary *request=[@{@"hash":reference[@"hash"],@"pixel_size":reference[@"pixel_size"],@"origin":@[@500,@40]} mutableCopy];
    NSDictionary *found=matchHeader(large,request);assert([found[@"matched"] boolValue]);
    assert([imageInfo(large,normalizedRect(found[@"rect"]),NO,NO)[@"hash"] isEqual:reference[@"hash"]]);
    request[@"origin"]=@[@503,@37];assert([matchHeader(large,request)[@"matched"] boolValue]);
    request[@"origin"]=@[@504,@40];assert(![matchHeader(large,request)[@"matched"] boolValue]);
    request[@"origin"]=@[@500,@40];request[@"hash"]=[@"0" stringByPaddingToLength:64 withString:@"0" startingAtIndex:0];
    assert(![matchHeader(large,request)[@"matched"] boolValue]);
    request[@"hash"]=@"invalid";BOOL rejected=NO;
    @try {matchHeader(large,request);} @catch(NSException *e) {rejected=[e.name isEqual:@"BAD_REGION"];}
    assert(rejected);
    for(int w=650;w<2048;w+=37) {
        CGRect r=CGRectMake(316.0/w,17.0/640,173.0/w,31.0/640);
        CGImageRef image=testImage(w*2,1280,316,17);
        CGRect p=pixelRect(image,r);assert(p.size.width==346 && p.size.height==62);
        CGImageRelease(image);
    }
    CGImageRelease(small);CGImageRelease(large);
    puts("Native title matching passed: translated/resized image, exact identity, OCR box drift, wrong title, invalid input, pixel rounding.");
    return 0;
}}
