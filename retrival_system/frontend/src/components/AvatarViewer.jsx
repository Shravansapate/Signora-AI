'use client';

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { AmbientLight, Box3, Color, DirectionalLight, PerspectiveCamera, Scene, Vector3, WebGLRenderer } from 'three';
import { PlaybackController } from '../motion/playback-controller.mjs';
import { revalidatePlan } from '../services/api.mjs';

const AvatarViewer = forwardRef(function AvatarViewer({ token, onState }, ref) {
  const host = useRef(null);
  const player = useRef(null);
  const latest = useRef({ token, onState });
  const [rendererError, setRendererError] = useState(null);
  latest.current = { token, onState };

  useImperativeHandle(ref, () => ({
    prepare: (plan) => {
      if (!player.current) throw new Error('The 3D renderer is unavailable. Reset the session to retry.');
      return player.current.prepare(plan, { token: latest.current.token });
    },
    start: () => player.current?.start(),
    cancel: () => player.current?.cancel(),
    grantLease: (deadline) => player.current?.grantLease(deadline),
    requestBoundaryStop: (reason) => player.current?.requestBoundaryStop(reason),
  }), []);

  useEffect(() => {
    const container = host.current;
    const scene = new Scene();
    scene.background = new Color('#e9edeb');
    const camera = new PerspectiveCamera(32, 1, 0.01, 1000);
    const ambient = new AmbientLight(0xffffff, 2.0);
    const key = new DirectionalLight(0xfff5e8, 3.2);
    const fill = new DirectionalLight(0xe3edff, 1.5);
    key.position.set(3, 4, 5);
    fill.position.set(-3, 2, 2);
    scene.add(ambient, key, fill);
    let renderer;
    try { renderer = new WebGLRenderer({ antialias: true, alpha: false }); }
    catch {
      setRendererError('3D rendering is unavailable. Enable WebGL and reset the session.');
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.setAttribute('aria-label', 'Persistent ISL avatar');
    renderer.domElement.setAttribute('role', 'img');
    container.appendChild(renderer.domElement);
    let frame;
    let previousTime;
    let avatarBounds;
    let lost = false;
    let sampledAt = 0;
    let needsRender = true;

    const frameAvatar = () => {
      if (!avatarBounds) return;
      const size = avatarBounds.getSize(new Vector3());
      const center = avatarBounds.getCenter(new Vector3());
      const halfFov = camera.fov * Math.PI / 360;
      const distance = Math.max(size.y / 2 / Math.tan(halfFov), size.x / 2 / (Math.tan(halfFov) * camera.aspect)) * 1.18 + size.z / 2;
      camera.near = Math.max(distance / 1000, 0.001);
      camera.far = distance * 100;
      camera.position.set(center.x, center.y, center.z + distance);
      camera.lookAt(center);
      camera.updateProjectionMatrix();
    };
    const controller = new PlaybackController({
      onState: (snapshot) => {
        needsRender = true;
        renderer.domElement.dataset.playbackState = snapshot.state;
        renderer.domElement.dataset.completedClips = String(snapshot.completed);
        renderer.domElement.dataset.clipCount = String(snapshot.total);
        renderer.domElement.dataset.manifestId = snapshot.manifestId ?? '';
        latest.current.onState(snapshot);
      },
      onAvatar: (root) => {
        needsRender = true;
        scene.add(root);
        let meshes = 0;
        const textures = new Set();
        root.traverse(node => {
          if (node.isMesh) meshes++;
          for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
            if (material) for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
          }
        });
        renderer.domElement.dataset.avatarInstance = root.uuid;
        renderer.domElement.dataset.avatarMeshes = String(meshes);
        renderer.domElement.dataset.avatarTextures = String(textures.size);
        renderer.domElement.dataset.maxTextureSize = String(Math.max(0, ...[...textures].map(texture => Math.max(texture.image.width, texture.image.height))));
        root.updateMatrixWorld(true);
        avatarBounds = new Box3().setFromObject(root);
        frameAvatar();
      },
      validateManifest: (plan, { signal }) => revalidatePlan(plan, { token: latest.current.token, signal }),
    });
    player.current = controller;

    const resize = () => {
      const width = container.clientWidth;
      const height = container.clientHeight;
      if (!width || !height) return;
      renderer.setSize(width, height, false);
      needsRender = true;
      camera.aspect = width / height;
      frameAvatar();
      camera.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();
    const render = (time) => {
      if (lost) return;
      if (previousTime !== undefined) controller.update(Math.min((time - previousTime) / 1000, 0.1));
      previousTime = time;
      if (needsRender || controller.state === 'PLAYING') {
        renderer.render(scene, camera);
        needsRender = false;
      }
      // Lightweight diagnostics from the rendered skeleton, for playback tracing.
      if (controller.root && time - sampledAt >= 250) {
        let hash = 2166136261;
        controller.root.traverse(node => {
          if (!node.isBone) return;
          for (const value of [...node.position.toArray(), ...node.quaternion.toArray()]) {
            hash = Math.imul(hash ^ Math.round(value * 100000), 16777619);
          }
        });
        renderer.domElement.dataset.poseHash = String(hash >>> 0);
        renderer.domElement.dataset.mixerTime = String(controller.mixer?.time ?? 0);
        sampledAt = time;
      }
      frame = requestAnimationFrame(render);
    };
    const contextLost = (event) => {
      event.preventDefault();
      lost = true;
      controller.cancel();
      player.current = null;
      setRendererError('The graphics context was lost. Reset the session before continuing.');
      latest.current.onState({ state: 'ERROR', index: -1, total: 0, completed: 0,
        error: { code: 'WEBGL_CONTEXT_LOST', message: 'Reset the session to restore 3D rendering.' } });
    };
    renderer.domElement.addEventListener('webglcontextlost', contextLost);
    frame = requestAnimationFrame(render);
    return () => {
      lost = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      renderer.domElement.removeEventListener('webglcontextlost', contextLost);
      controller.dispose();
      player.current = null;
      scene.clear();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, []);

  return <div className="avatar-host" ref={host}>
    {rendererError && <p className="renderer-error" role="alert">{rendererError}</p>}
  </div>;
});

export default AvatarViewer;
