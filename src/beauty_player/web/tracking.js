// MediaPipe FaceMesh indices; geometry is independent of playback and rendering.
export function faceGeometry(points, aspect) {
  if (!Array.isArray(points) || points.length < 468) return null;
  const at = i => [points[i].x, points[i].y];
  if (points.some(p => !Number.isFinite(p.x) || !Number.isFinite(p.y))) return null;
  const mid = (a,b) => [(a[0]+b[0])/2,(a[1]+b[1])/2];
  const left=at(234), right=at(454), top=at(10), bottom=at(152), nose=at(1);
  const center=mid(top,bottom);
  const dx=(right[0]-left[0])*aspect, dy=right[1]-left[1];
  const width=Math.hypot(dx,dy), height=Math.hypot((top[0]-bottom[0])*aspect,top[1]-bottom[1]);
  if (width<.025 || height<.04) return null;
  const l=Math.hypot((nose[0]-left[0])*aspect,nose[1]-left[1]);
  const r=Math.hypot((nose[0]-right[0])*aspect,nose[1]-right[1]);
  return {face:[...center,width*.55,height*.55],axis:[dx/width,dy/width],
    eyes:[...mid(at(33),at(133)),...mid(at(362),at(263))],
    cheeks:[...at(132),...at(361)],mouth:at(13),
    shape:Math.min(l,r)/Math.max(l,r)>.32 ? 1:0};
}

export function validTracking(result, generation, currentTime) {
  return result && result.generation===generation && Math.abs(result.time-currentTime)<.22;
}
