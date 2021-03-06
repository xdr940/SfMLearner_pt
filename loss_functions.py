from __future__ import division
import torch
from torch import nn
import torch.nn.functional as F
from inverse_warp import inverse_warp
import matplotlib.pyplot as plt

def photometric_reconstruction_loss(tgt_img, ref_imgs, intrinsics,
                                    depth, explainability_mask, pose,
                                    rotation_mode='euler', padding_mode='zeros'):
    def one_scale(depth, explainability_mask):
        assert(explainability_mask is None or depth.size()[2:] == explainability_mask.size()[2:])
        assert(pose.size(1) == len(ref_imgs))

        reconstruction_loss = 0
        b, _, h, w = depth.size()#6,1,128,416
        downscale = tgt_img.size(2)/h#6,3,128,416, 128/128==1

        tgt_img_scaled = F.interpolate(tgt_img, (h, w), mode='area')#bs,c,h,w
        ref_imgs_scaled = [F.interpolate(ref_img, (h, w), mode='area') for ref_img in ref_imgs]#length sq-length-1 list of bs,c,h,w
        intrinsics_scaled = torch.cat((intrinsics[:, 0:2]/downscale, intrinsics[:, 2:]), dim=1)#6,3,3 to 6, 3, 3
        #上面三个值完全没变
        warped_imgs = []
        diff_maps = []

        for i, ref_img in enumerate(ref_imgs_scaled):# sq-lenth -1,i 从所有refs 里面遍历
            current_pose = pose[:, i]#bs,sq-length -1 ,6

            #bs,c,h,w
            ref_img_warped, valid_points = inverse_warp(ref_img, depth[:,0], current_pose,#depth b,c,h,w-->b,h,w, 其他通道不要了
                                                        intrinsics_scaled,
                                                        rotation_mode, padding_mode)

            #plt.imsave('a.png',ref_img_warped[0,0,:,:].cpu().data.numpy())# 与ref-img高度重合
            #bs,c,h,w
            diff = (tgt_img_scaled - ref_img_warped) * valid_points.unsqueeze(1).float()
           # plt.imsave(str(i)+'a.png',diff[0,0,:,:].cpu().data.numpy())# 与ref-img高度重合

            if explainability_mask is not None:# lenthg-4 list of (bs,sq-lenth-1,h,w)
                diff = diff * explainability_mask[:,i:i+1].expand_as(diff)

            # 1.loss add
            reconstruction_loss += diff.abs().mean()# 0d tensor
            assert((reconstruction_loss == reconstruction_loss).item() == 1)
            #2.
            warped_imgs.append(ref_img_warped[0])#bs中取第一个?

            #
            diff_maps.append(diff[0])
            #       0d tensor;  sq-lenth-1 -lenth list of 3,h,w; sq-lenth-1 -lenth list of 3,h,w
        return reconstruction_loss, warped_imgs, diff_maps

    warped_results, diff_results = [], []

    if type(explainability_mask) not in [tuple, list]:#这两步是为了将如果输出一种尺寸，也能吻合下面的list操作
        explainability_mask = [explainability_mask]
    if type(depth) not in [list, tuple]:#这里的depth list 4 (四个scale)，bs,1,h,w
        depth = [depth]

    total_loss = 0
    for d, mask in zip(depth, explainability_mask):#四种尺度
        loss, warped, diff = one_scale(d, mask)
        total_loss += loss#0d tensor
        warped_results.append(warped)#warped: sq-lenth-1-lenth list of (3,h*,w*)
        diff_results.append(diff)
    return total_loss, warped_results, diff_results


def explainability_loss(mask):
    if type(mask) not in [tuple, list]:
        mask = [mask]
    loss = 0
    for mask_scaled in mask:
        ones_var = torch.ones_like(mask_scaled)
        loss += nn.functional.binary_cross_entropy(mask_scaled, ones_var)
    return loss


def smooth_loss(pred_map):
    def gradient(pred):#前面两个维度应该是图片数量(batch_size)和通道
        D_dy = pred[:, :, 1:] - pred[:, :, :-1]
        D_dx = pred[:, :, :, 1:] - pred[:, :, :, :-1]
        return D_dx, D_dy

    if type(pred_map) not in [tuple, list]:
        pred_map = [pred_map]

    loss = 0
    weight = 1.
    #process 
    for scaled_map in pred_map:
        dx, dy = gradient(scaled_map)
        dx2, dxdy = gradient(dx)
        dydx, dy2 = gradient(dy)
        loss += (dx2.abs().mean() + dxdy.abs().mean() + dydx.abs().mean() + dy2.abs().mean())*weight
        weight /= 2.3  # don't ask me why it works better
    return loss


@torch.no_grad()#这个函数内定义的变量都不求导
def compute_errors(gt, pred, crop=True):
    abs_diff, abs_rel, sq_rel, a1, a2, a3 = 0,0,0,0,0,0
    batch_size = gt.size(0)

    '''
    crop used by Garg ECCV16 to reprocude Eigen NIPS14 results
    construct a mask of False values, with the same size as target
    and then set to True values inside the crop
    '''
    if crop:
        crop_mask = gt[0] != gt[0]
        y1,y2 = int(0.40810811 * gt.size(1)), int(0.99189189 * gt.size(1))
        x1,x2 = int(0.03594771 * gt.size(2)), int(0.96405229 * gt.size(2))
        crop_mask[y1:y2,x1:x2] = 1

    for current_gt, current_pred in zip(gt, pred):
        valid = (current_gt > 0) & (current_gt < 80)
        if crop:
            valid = valid & crop_mask

        valid_gt = current_gt[valid]
        valid_pred = current_pred[valid].clamp(1e-3, 80)

        valid_pred = valid_pred * torch.median(valid_gt)/torch.median(valid_pred)

        thresh = torch.max((valid_gt / valid_pred), (valid_pred / valid_gt))
        a1 += (thresh < 1.25).float().mean()
        a2 += (thresh < 1.25 ** 2).float().mean()
        a3 += (thresh < 1.25 ** 3).float().mean()

        abs_diff += torch.mean(torch.abs(valid_gt - valid_pred))
        abs_rel += torch.mean(torch.abs(valid_gt - valid_pred) / valid_gt)

        sq_rel += torch.mean(((valid_gt - valid_pred)**2) / valid_gt)

    return [metric.item() / batch_size for metric in [abs_diff, abs_rel, sq_rel, a1, a2, a3]]
