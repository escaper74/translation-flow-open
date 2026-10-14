/* Face coordinate warps adapted from GPUPixel's face_reshape_filter.cc.
 * Copyright (c) 2021 PixPark. Apache-2.0; see ../THIRD_PARTY.txt.
 * Modifications: MediaPipe geometry, two faces, bounded strengths, face masks.
 * Smoothing/tonal adjustment below is an independent local implementation.
 */
const vertex=`attribute vec2 p; varying vec2 uv; void main(){uv=vec2(p.x*.5+.5,.5-p.y*.5);gl_Position=vec4(p,0.,1.);}`;
const fragment=`precision highp float;
varying vec2 uv; uniform sampler2D image; uniform vec2 size;
uniform vec4 controls; uniform int count;
uniform vec4 face[2]; uniform vec2 axis[2]; uniform vec4 eyes[2];
uniform vec4 cheeks[2]; uniform vec2 mouth[2]; uniform float shape[2];
vec2 metric(vec2 v){return v*vec2(size.x/size.y,1.);}
vec2 warp(vec2 p,vec2 origin,vec2 target,float strength,float radius){
 float ratio=clamp(1.-length(metric(p-origin))/max(radius,.001),0.,1.);
 return p-(target-origin)*ratio*ratio*strength;
}
vec2 eyeWarp(vec2 p,vec2 center,float radius,float strength){
 float r=length(metric(p-center))/max(radius,.001);
 float weight=1.-clamp(1.-r*r,0.,1.)*strength;
 return center+(p-center)*weight;
}
void main(){
 vec2 p=uv; float mask=0.;
 for(int i=0;i<2;i++){
  if(i>=count)break;
  vec4 f=face[i];
  float amount=controls.z*.16*shape[i];
  p=warp(p,cheeks[i].xy,f.xy,amount,f.z*.8);
  p=warp(p,cheeks[i].zw,f.xy,amount,f.z*.8);
  p=eyeWarp(p,eyes[i].xy,f.z*.33,controls.w*.2*shape[i]);
  p=eyeWarp(p,eyes[i].zw,f.z*.33,controls.w*.2*shape[i]);
 }
 vec3 original=texture2D(image,p).rgb;
 for(int i=0;i<2;i++){
  if(i>=count)break;
  vec4 f=face[i]; vec2 d=metric(p-f.xy);
  vec2 local=vec2(dot(d,axis[i]),dot(d,vec2(-axis[i].y,axis[i].x)))/f.zw;
  float region=1.-smoothstep(.72,1.,length(local));
  float eyeProtect=min(smoothstep(f.z*.12,f.z*.25,length(metric(p-eyes[i].xy))),smoothstep(f.z*.12,f.z*.25,length(metric(p-eyes[i].zw))));
  float mouthProtect=smoothstep(f.z*.15,f.z*.32,length(metric(p-mouth[i])));
  mask=max(mask,region*eyeProtect*mouthProtect);
 }
 vec3 color=original;
 if(mask>0.001 && controls.x>0.){
  vec2 step=vec2(2.5)/size;
  vec3 sum=original; float total=1.;
  for(int x=-2;x<=2;x++){for(int y=-2;y<=2;y++){
   if(x==0 && y==0)continue;
   vec3 sampleColor=texture2D(image,p+vec2(float(x),float(y))*step).rgb;
   vec3 delta=sampleColor-original;
   float weight=exp(-dot(delta,delta)*70.-float(x*x+y*y)*.22);
   sum+=sampleColor*weight;total+=weight;
  }}
  color=mix(original,sum/total,controls.x*.85*mask);
 }
 vec3 lighter=pow(max(color,vec3(0.)),vec3(.8));
 color=mix(color,lighter,controls.y*.8*mask);
 gl_FragColor=vec4(color,1.);
}`;

export class BeautyRenderer {
  constructor(canvas){
    this.canvas=canvas;
    this.gl=canvas.getContext('webgl',{alpha:false,antialias:false,preserveDrawingBuffer:false});
    if(!this.gl)throw Error('浏览器未提供 WebGL；请启用浏览器图形加速后重新打开。');
    const gl=this.gl;
    const compile=(type,source)=>{const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};
    this.program=gl.createProgram();gl.attachShader(this.program,compile(gl.VERTEX_SHADER,vertex));gl.attachShader(this.program,compile(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(this.program);
    if(!gl.getProgramParameter(this.program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(this.program));
    gl.useProgram(this.program);
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
    const pos=gl.getAttribLocation(this.program,'p');gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
    this.texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,this.texture);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    this.uniform={};for(const name of ['image','size','controls','count','face[0]','axis[0]','eyes[0]','cheeks[0]','mouth[0]','shape[0]'])this.uniform[name]=gl.getUniformLocation(this.program,name);
  }
  draw(source,width,height,faces,controls){
    const gl=this.gl,u=this.uniform;
    // Cap display processing at 1080p; the source and audio are untouched.
    const scale=Math.min(1,1920/width,1080/height),w=Math.round(width*scale),h=Math.round(height*scale);
    if(this.canvas.width!==w||this.canvas.height!==h){this.canvas.width=w;this.canvas.height=h;gl.viewport(0,0,w,h);}
    gl.useProgram(this.program);gl.bindTexture(gl.TEXTURE_2D,this.texture);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,source);
    gl.uniform1i(u.image,0);gl.uniform2f(u.size,width,height);gl.uniform4fv(u.controls,controls);
    const data=faces.slice(0,2);gl.uniform1i(u.count,data.length);
    for(const [key,length,set] of [['face',4,'uniform4fv'],['axis',2,'uniform2fv'],['eyes',4,'uniform4fv'],['cheeks',4,'uniform4fv'],['mouth',2,'uniform2fv'],['shape',1,'uniform1fv']]){
      const values=new Float32Array(2*length);data.forEach((f,i)=>values.set(length===1?[f[key]]:f[key],i*length));gl[set](u[key+'[0]'],values);
    }
    gl.drawArrays(gl.TRIANGLES,0,6);
  }
}
