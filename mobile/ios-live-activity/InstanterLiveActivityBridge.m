#import <React/RCTBridgeModule.h>

@interface RCT_EXTERN_MODULE(InstanterLiveActivity, NSObject)
RCT_EXTERN_METHOD(start:(nonnull NSNumber *)waiting)
RCT_EXTERN_METHOD(end)
@end
