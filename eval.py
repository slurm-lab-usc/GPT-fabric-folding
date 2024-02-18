import argparse
import sys
from datetime import date
import numpy as np
from softgym.envs.foldenv import FoldEnv
from utils.visual import get_world_coord_from_pixel, action_viz, nearest_to_mask, save_video
import pyflex
from utils.setup_model import get_configs, setup_model
import torch
import os
import pickle
from tqdm import tqdm
from einops import rearrange
from utils.load_configs import get_configs
import imageio
from skimage.transform import resize
from utils.gpt_utils import system_prompt, get_user_prompt, parse_output, analyze_images_gpt
from openai import OpenAI
from slurm_utils import find_corners, find_pixel_center_of_cloth

from openai import OpenAI
client = OpenAI(api_key="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")

def get_mask(depth):
    mask = depth.copy()
    mask[mask > 0.646] = 0
    mask[mask != 0] = 1
    return mask


def preprocess(depth):
    mask = get_mask(depth)
    depth = depth * mask
    return depth


def main():
    parser = argparse.ArgumentParser(description="Evaluate Foldsformer")
    parser.add_argument("--gui", action="store_true", help="Run headless or not")
    parser.add_argument("--task", type=str, default="DoubleTriangle", help="Task name")
    parser.add_argument("--img_size", type=int, default=128, help="Size of rendered image")
    parser.add_argument("--model_config", type=str, help="Evaluate which model")
    parser.add_argument("--model_file", type=str, help="Evaluate which trained model")
    parser.add_argument("--cached", type=str, help="Cached filename")
    parser.add_argument('--save_video_dir', type=str, default='./videos/', help='Path to the saved video')
    parser.add_argument('--save_vid', type=bool, default=False, help='Decide whether to save video or not')
    parser.add_argument('--user_points', type=str, default="user", help='Choose one of [user | llm | foldsformer]')
    args = parser.parse_args()

    # task
    task = args.task
    if task == "CornersEdgesInward":
        frames_idx = [0, 1, 2, 3, 4]
        steps = 4
    elif task == "AllCornersInward":
        frames_idx = [0, 1, 2, 3, 4]
        steps = 4
    elif task == "DoubleStraight":
        frames_idx = [0, 1, 2, 3, 3]
        steps = 3
    elif task == "DoubleTriangle":
        frames_idx = [0, 1, 1, 2, 2]
        steps = 2

    # Writing things to the specified log file
    output_file_path = os.path.join("logs", args.task, args.cached)
    if not os.path.exists(output_file_path):
        os.makedirs(output_file_path)
    output_file = os.path.join(output_file_path, str(date.today()) + ".log")
    sys.stdout = open(output_file, 'w', buffering=1)

    # env settings
    cached_path = os.path.join("cached configs", args.cached + ".pkl")
    env = FoldEnv(cached_path, gui=args.gui, render_dim=args.img_size)

    # create transformer model & load parameters
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_config_path = os.path.join("train", "train configs", args.model_config + ".yaml")
    configs = get_configs(model_config_path)
    trained_model_path = os.path.join("train", "trained model", configs["save_model_name"], args.model_file + ".pth")
    net = setup_model(configs)
    net = net.to(device)
    net.load_state_dict(torch.load(trained_model_path)["model"])
    print(f"load trained model from {trained_model_path}")
    net.eval()

    # set goal
    depth_load_path = os.path.join("data", "demo", args.task, "depth")
    goal_frames = []
    for i in frames_idx:
        frame = imageio.imread(os.path.join(depth_load_path, str(i) + ".png")) / 255
        frame = resize(frame, (args.img_size, args.img_size), anti_aliasing=True)
        frame = torch.FloatTensor(preprocess(frame)).unsqueeze(0).unsqueeze(0)
        goal_frames.append(frame)
    goal_frames = torch.cat(goal_frames, dim=0)

    for config_id in tqdm(range(env.num_configs)):
        rgb_save_path = os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "rgb")
        depth_save_path = os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "depth")
        if not os.path.exists(rgb_save_path):
            os.makedirs(rgb_save_path)
        if not os.path.exists(depth_save_path):
            os.makedirs(depth_save_path)

        # record action's pixel info
        test_pick_pixels = []
        test_place_pixels = []
        rgbs = []

        # env settings
        env.reset(config_id=config_id)
        camera_params = env.camera_params

        # initial state
        rgb, depth = env.render_image()
        depth_save = depth.copy() * 255
        depth_save = depth_save.astype(np.uint8)
        imageio.imwrite(os.path.join(depth_save_path, "0.png"), depth_save)
        imageio.imwrite(os.path.join(rgb_save_path, "0.png"), rgb)
        rgbs.append(rgb)
        
        image_path = os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "depth", "0.png")
        cloth_center = find_pixel_center_of_cloth(image_path)

        for i in range(steps):
            print("------------------------------------------------------")
            print("Currently in {} step of {} config".format(i, config_id))

            current_state = torch.FloatTensor(preprocess(depth)).unsqueeze(0).unsqueeze(0)
            current_frames = torch.cat((current_state, goal_frames), dim=0).unsqueeze(0)
            current_frames = rearrange(current_frames, "b t c h w -> b c t h w")
            current_frames = current_frames.to(device)

            # get action based on the input asked to the user
            if args.user_points == "user":
                pick_str = input("Enter the pick pixel in the form [x,y]: ")
                test_pick_pixel = np.array(tuple(map(float, pick_str.strip("[]").split(','))))

                place_str = input("Enter the place pixel in the form [x,y]: ")
                test_place_pixel = np.array(tuple(map(float, place_str.strip("[]").split(','))))
            
            # get action based on what our LLM API integration predicts 
            elif args.user_points == "llm":
                # Detecting corners for the current cloth configuration
                image_path = os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "depth", str(i) + ".png")
                cloth_corners = find_corners(image_path)

                # Getting the template folding instruction images from the demonstrations
                demo_root_path = os.path.join("data", "demo", args.task, "rgbviz")
                # init_image = os.path.join(demo_root_path, str(0) + ".png")
                start_image = os.path.join(demo_root_path, str(i) + ".png")
                last_image = os.path.join(demo_root_path, str(i+1) + ".png")

                # Generating the instruction by analyzing the images
                instruction = analyze_images_gpt([start_image, last_image])

                # getting the system and user prompts for our given request
                user_prompt = get_user_prompt(cloth_corners, cloth_center, True, instruction, args.task)
                print(user_prompt)
                response = client.chat.completions.create(
                    model="gpt-4-1106-preview",
                    messages=[
                        {
                        "role": "system",
                        "content": system_prompt
                        },
                        {
                        "role": "user",
                        "content": user_prompt
                        }
                    ],
                    temperature=0,
                    max_tokens=769,
                    top_p=1,
                    frequency_penalty=0,
                    presence_penalty=0
                )
                # Parsing the above output to get the pixel coorindates for pick and place
                test_pick_pixel, test_place_pixel = parse_output(response.choices[0].message.content)
                print(response.choices[0].message.content)
            else:
                pickmap, placemap = net(current_frames)
                pickmap = torch.sigmoid(torch.squeeze(pickmap))
                placemap = torch.sigmoid(torch.squeeze(placemap))
                pickmap = pickmap.detach().cpu().numpy()
                placemap = placemap.detach().cpu().numpy()

                test_pick_pixel = np.array(np.unravel_index(pickmap.argmax(), pickmap.shape))
                test_place_pixel = np.array(np.unravel_index(placemap.argmax(), placemap.shape))

                mask = get_mask(depth)
                test_pick_pixel_mask = nearest_to_mask(test_pick_pixel[0], test_pick_pixel[1], mask)
                test_pick_pixel[0], test_pick_pixel[1] = test_pick_pixel_mask[1], test_pick_pixel_mask[0]
                test_place_pixel[0], test_place_pixel[1] = test_place_pixel[1], test_place_pixel[0]
            
            # Appending the chosen pickels to the list of the pick and place pixels
            test_pick_pixels.append(test_pick_pixel)
            test_place_pixels.append(test_place_pixel)

            # Printing the pixels chosen/computed
            print("The Pick and the place pixels", test_pick_pixel, test_place_pixel)

            # convert the pixel cords into world cords
            test_pick_pos = get_world_coord_from_pixel(test_pick_pixel, depth, camera_params)
            test_place_pos = get_world_coord_from_pixel(test_place_pixel, depth, camera_params)

            # pick & place
            env.pick_and_place(test_pick_pos.copy(), test_place_pos.copy())

            # render & update frames & save
            rgb, depth = env.render_image()
            depth_save = depth.copy() * 255
            depth_save = depth_save.astype(np.uint8)
            imageio.imwrite(os.path.join(depth_save_path, str(i + 1) + ".png"), depth_save)
            imageio.imwrite(os.path.join(rgb_save_path, str(i + 1) + ".png"), rgb)
            rgbs.append(rgb)

        particle_pos = pyflex.get_positions().reshape(-1, 4)[:, :3]
        with open(os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "info.pkl"), "wb+") as f:
            data = {"pick": test_pick_pixels, "place": test_place_pixels, "pos": particle_pos}
            pickle.dump(data, f)

        # action viz
        save_folder = os.path.join("eval result", args.task, args.cached, str(date.today()), str(config_id), "rgbviz")
        if not os.path.exists(save_folder):
            os.makedirs(save_folder)

        for i in range(steps + 1):
            if i < steps:
                img = action_viz(rgbs[i], test_pick_pixels[i], test_place_pixels[i])
            else:
                img = rgbs[i]
            imageio.imwrite(os.path.join(save_folder, str(i) + ".png"), img)

        # Save a video from the list of the image arrays
        if args.save_vid:
            save_vid_path = os.path.join(args.save_video_dir, args.task, args.cached, str(date.today()))
            if not os.path.exists(save_vid_path):
                os.makedirs(save_vid_path)
            save_video(env.rgb_array, os.path.join(save_vid_path, str(config_id)))
        env.rgb_array = []

if __name__ == "__main__":
    main()
