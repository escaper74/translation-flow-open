let model;
async function initialize() {
  // MediaPipe 0.10.x loads its WASM glue with importScripts: classic worker,
  // with a dynamic ESM import, keeps this off the UI thread.
  const {FaceLandmarker, FilesetResolver}=await import('./runtime/vision_bundle.mjs');
  const files=await FilesetResolver.forVisionTasks(new URL('./runtime/wasm',self.location.href).href);
  model=await FaceLandmarker.createFromOptions(files,{baseOptions:{
    modelAssetPath:new URL('./runtime/face_landmarker.task',self.location.href).href,delegate:'CPU'},
    runningMode:'VIDEO',numFaces:2,minFaceDetectionConfidence:.6,minFacePresenceConfidence:.6,minTrackingConfidence:.6});
  postMessage({type:'ready'});
}
self.onmessage=event=>{
  const {bitmap,time,generation,stamp}=event.data;
  if (!bitmap) return;
  try {
    const before=performance.now();
    const result=model.detectForVideo(bitmap,stamp);
    postMessage({type:'faces',faces:result.faceLandmarks,time,generation,elapsed:performance.now()-before});
  } catch(error) {postMessage({type:'error',message:String(error.message)});}
  finally {bitmap.close();}
};
initialize().catch(error=>postMessage({type:'error',message:String(error.message)}));
