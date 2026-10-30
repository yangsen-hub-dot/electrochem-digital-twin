"""Continuous fixed-orientation triangle sweep against a static AABB, metres.

Each translated triangle sweeps a convex triangular prism. Separating axes
cover box faces, prism faces and edge cross-products. This checks the whole
translation segment, rather than only its sampled endpoints. Rotation is
handled separately by the executor's conservative rotation gate.
"""
import numpy as np


def swept_triangles_in_aabb(vertices,triangles,translation,lower,upper,tolerance=1e-6):
    vertices=np.asarray(vertices,dtype=float);faces=np.asarray(triangles,dtype=int)
    delta=np.asarray(translation,dtype=float)
    lower=np.asarray(lower,dtype=float);upper=np.asarray(upper,dtype=float)
    if (vertices.ndim!=2 or vertices.shape[1]!=3 or faces.ndim!=2 or faces.shape[1]!=3
            or delta.shape!=(3,) or lower.shape!=(3,) or upper.shape!=(3,)
            or not np.isfinite(vertices).all() or not np.isfinite(np.r_[delta,lower,upper]).all()
            or not len(faces) or np.any(faces<0) or np.any(faces>=len(vertices))):
        raise ValueError('invalid swept-mesh geometry')
    center=(lower+upper)/2;half=(upper-lower)/2-float(tolerance)
    if np.any(half<=0):return False
    tri=vertices[faces]-center
    prism=np.concatenate((tri,tri+delta),axis=1)
    possible=np.all((prism.min(axis=1)<=half)&(prism.max(axis=1)>=-half),axis=1)
    prism=prism[possible];tri=tri[possible]
    if not len(prism):return False
    edges=np.roll(tri,-1,axis=1)-tri
    axes=[np.broadcast_to(axis,(len(tri),3)) for axis in np.eye(3)]
    axes.append(np.cross(edges[:,0],edges[:,1]))
    axes.extend(np.cross(edges[:,i],delta) for i in range(3))
    axes.extend(np.cross(edges[:,i],axis) for i in range(3) for axis in np.eye(3))
    axes.extend(np.broadcast_to(np.cross(delta,axis),(len(tri),3)) for axis in np.eye(3))
    intersects=np.ones(len(tri),dtype=bool)
    for axis in axes:
        projections=np.einsum('nkj,nj->nk',prism,axis)
        radius=np.abs(axis)@half
        intersects&=(projections.min(axis=1)<=radius)&(projections.max(axis=1)>=-radius)
        if not intersects.any():return False
    return bool(intersects.any())
